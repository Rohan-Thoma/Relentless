import asyncio
import logging

from jobspy import scrape_jobs
import pandas as pd

from src.config import load_config
from src.ingestors.common import clean_html, insert_job, matches_basic_filters
from src.ingestors.freshness import is_timestamp_fresh
from src.ingestors.location_policy import (
    derive_jobboard_queries_from_policy,
    extract_job_location_facts,
)
from src.ingestors.url_resolver import register_discovered_board
from src.ingestors.watermarks import compute_delta_hours, update_watermark
from src.settings_store import get_master_profile

logger = logging.getLogger(__name__)


def get_jobboards_parameters(
    search_terms: list[str] | None = None,
    locations: list[str] | None = None,
    sites: list[str] | None = None,
    results_wanted: int | None = None,
    hours_old: int | None = None,
) -> tuple[list[str], list[str], list[str], int, int]:
    cfg = load_config()
    jb_cfg = getattr(cfg, "jobboards", None)

    # 1. Target sites from config
    target_sites = list(sites) if sites else []
    if not target_sites:
        if jb_cfg and jb_cfg.sites:
            target_sites = list(jb_cfg.sites)
        else:
            target_sites = ["naukri", "linkedin", "indeed"]

    # 2. Results count and freshness from config
    res_wanted = results_wanted
    if res_wanted is None:
        res_wanted = jb_cfg.results_wanted if (jb_cfg and jb_cfg.results_wanted) else 20

    hrs = hours_old
    if hrs is None:
        hrs = jb_cfg.hours_old if (jb_cfg and jb_cfg.hours_old) else 72

    # 3. Search terms: prioritize explicit config.jobboards.search_terms
    terms = list(search_terms) if search_terms else []
    if not terms:
        if jb_cfg and jb_cfg.search_terms:
            terms.extend(jb_cfg.search_terms)
        else:
            if cfg.candidate_preferences and cfg.candidate_preferences.target_role_keywords:
                for kw in cfg.candidate_preferences.target_role_keywords:
                    clean_kw = kw.strip()
                    if clean_kw and ("engineer" in clean_kw.lower() or " " in clean_kw):
                        title_kw = clean_kw.title()
                        if title_kw not in terms:
                            terms.append(title_kw)

            try:
                profile = get_master_profile()
                if profile:
                    app_defaults = profile.get("application_defaults") or {}
                    curr_title = app_defaults.get("current_title")
                    if curr_title and curr_title not in terms:
                        terms.append(curr_title)
                    for exp in profile.get("experience", []):
                        role = exp.get("role")
                        if role and role not in terms:
                            terms.append(role)
            except Exception as e:
                logger.warning("Jobboards search_terms fallback could not read master_profile: %s", e)

    # 4. Locations: prioritize explicit arg -> config.jobboards.locations -> derived from location_policy
    locs = list(locations) if locations else []
    if not locs:
        if jb_cfg and jb_cfg.locations:
            locs.extend(jb_cfg.locations)
        else:
            policy = cfg.effective_location_policy()
            for q in derive_jobboard_queries_from_policy(policy):
                locs.append(q["location"])

    terms = list(dict.fromkeys(terms))
    locs = list(dict.fromkeys(locs))
    target_sites = list(dict.fromkeys(target_sites))

    return terms, locs, target_sites, res_wanted, hrs


def _get_row_str(row: pd.Series, key: str) -> str:
    if key not in row:
        return ""
    val = row[key]
    if pd.isna(val) or val is None:
        return ""
    return str(val).strip()


def _get_row_bool(row: pd.Series, key: str) -> bool | None:
    if key not in row:
        return None
    val = row[key]
    if pd.isna(val) or val is None:
        return None
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    if s in ("true", "1", "yes"):
        return True
    if s in ("false", "0", "no"):
        return False
    return None


def _is_remote_query(loc: str, policy) -> bool:
    low = (loc or "").strip().lower()
    if low in ("remote", "anywhere", "worldwide", "global"):
        return True
    # If every active location rule requires remote-only, pass is_remote=True to JobSpy!
    if policy and policy.rules:
        all_remote_only = all(
            {m.strip().lower() for m in r.work_modes} == {"remote"}
            for r in policy.rules
            if r.work_modes
        )
        if all_remote_only:
            return True
    return False


def _scrape_sync(
    site: str | list[str] | None = None,
    sites: list[str] | None = None,
    search_term: str = "",
    location: str = "",
    results_wanted: int = 20,
    hours_old: int = 72,
    country_indeed: str | None = None,
    is_remote: bool = False,
    **kwargs,
) -> pd.DataFrame:
    target = sites or (site if isinstance(site, list) else ([site] if site else ["naukri", "linkedin", "indeed"]))
    resolved_country = (country_indeed or "").strip()
    if not resolved_country:
        try:
            jb_cfg = getattr(load_config(), "jobboards", None)
            resolved_country = (getattr(jb_cfg, "country_indeed", None) or "").strip()
        except Exception:
            resolved_country = ""
    if not resolved_country:
        resolved_country = "india"
    try:
        df = scrape_jobs(
            site_name=target,
            search_term=search_term,
            location=location,
            results_wanted=results_wanted,
            hours_old=hours_old,
            country_indeed=resolved_country,
            is_remote=is_remote,
            linkedin_fetch_description=True,
            verbose=0,
        )
        return df if df is not None and not df.empty else pd.DataFrame()
    except Exception as e:
        logger.error(
            "JobSpy scrape failed for sites=%s, term='%s', loc='%s', is_remote=%s: %s",
            target,
            search_term,
            location,
            is_remote,
            e,
        )
        return pd.DataFrame()


async def poll_jobboards(
    search_terms: list[str] | None = None,
    locations: list[str] | None = None,
    sites: list[str] | None = None,
    results_wanted: int | None = None,
    hours_old: int | None = None,
) -> int:
    cfg = load_config()
    policy = cfg.effective_location_policy()

    terms, locs, target_sites, res_count, hrs = get_jobboards_parameters(
        search_terms=search_terms,
        locations=locations,
        sites=sites,
        results_wanted=results_wanted,
        hours_old=hours_old,
    )

    if not terms or not locs:
        msg = (
            "Jobboards poller cannot run without search_terms and locations. "
            "Configure them in 06 // CONFIG & VAULT -> Engine Config or set up your Master Profile."
        )
        logger.error(msg)
        raise ValueError(msg)

    combos = [(term, loc) for term in terms for loc in locs]
    total_combos = len(combos)
    sites_label = ", ".join(target_sites)
    print(
        f"  📋 [JobSpy Plan] {total_combos} queries across [{sites_label}] ({len(terms)} terms × {len(locs)} locations)",
        flush=True,
    )

    total_added = 0
    for idx, (term, loc) in enumerate(combos, 1):
        remote_flag = _is_remote_query(loc, policy)
        wm_key = f"jobspy:{sites_label}:{term.lower()}:{loc.lower()}:r{int(remote_flag)}"
        effective_hrs = compute_delta_hours(wm_key, default_hours=hrs)

        print(
            f"  🔍 [JobSpy {idx}/{total_combos}] Querying '{term}' in '{loc}' (remote={remote_flag}, window={effective_hrs}h) on [{sites_label}]...",
            flush=True,
        )
        try:
            df = await asyncio.to_thread(
                _scrape_sync,
                sites=target_sites,
                search_term=term,
                location=loc,
                results_wanted=res_count,
                hours_old=effective_hrs,
                is_remote=remote_flag,
            )
            if df is None or df.empty:
                print(f"    ↳ No postings returned for '{term}' in '{loc}'.", flush=True)
                await asyncio.to_thread(update_watermark, wm_key, None, 0)
                continue

            site_added = 0
            site_counts = {}
            for _, row in df.iterrows():
                raw_date = row.get("date_posted") if hasattr(row, "get") else row.get("date_posted", None)
                if not is_timestamp_fresh(raw_date, float(hrs)):
                    continue

                title = _get_row_str(row, "title")
                company = _get_row_str(row, "company")
                location_val = _get_row_str(row, "location")
                description = _get_row_str(row, "description")
                job_url = _get_row_str(row, "job_url")
                job_url_direct = _get_row_str(row, "job_url_direct")
                row_site = _get_row_str(row, "site") or "jobboard"
                row_is_remote = _get_row_bool(row, "is_remote")
                ext_id = _get_row_str(row, "id") or None

                site_counts[row_site] = site_counts.get(row_site, 0) + 1

                apply_url = job_url_direct or job_url
                if not title or not apply_url:
                    continue

                await asyncio.to_thread(register_discovered_board, apply_url)

                clean_jd = clean_html(description)
                effective_loc = location_val or (loc if remote_flag else "")
                if matches_basic_filters(title, effective_loc, jd_text=clean_jd, is_remote=row_is_remote, cfg=cfg):
                    facts = extract_job_location_facts(
                        title=title,
                        location=effective_loc,
                        jd_text=clean_jd,
                        is_remote_hint=row_is_remote,
                        candidate_country=policy.candidate_country,
                        check_jd_geo_blockers=policy.check_jd_geo_blockers,
                    )
                    inserted = await asyncio.to_thread(
                        insert_job,
                        None,
                        f"jobspy_{row_site}",
                        company or "Unknown",
                        title,
                        location_val or loc,
                        apply_url,
                        clean_jd,
                        original_source_url=job_url if (job_url_direct and job_url and job_url != job_url_direct) else None,
                        work_mode=facts.work_mode,
                        external_job_id=ext_id,
                    )
                    if inserted:
                        site_added += 1

            total_added += site_added
            await asyncio.to_thread(update_watermark, wm_key, None, len(df))
            counts_breakdown = ", ".join(f"{s}: {c}" for s, c in site_counts.items())
            print(
                f"    ↳ Fetched {len(df)} postings ({counts_breakdown}) -> +{site_added} new matching jobs saved.",
                flush=True,
            )
        except Exception as e:
            print(f"    ❌ Error during '{term}' in '{loc}': {e}", flush=True)
            logger.warning("Error processing JobSpy batch: %s", e)

    print(
        f"  ✅ [JobSpy Summary] Completed all {total_combos} searches -> +{total_added} total new jobs saved.",
        flush=True,
    )
    return total_added


if __name__ == "__main__":
    asyncio.run(poll_jobboards())
