import asyncio
import logging
import pandas as pd

from jobspy import scrape_jobs

from src.config import load_config
from src.ingestors.common import clean_html, insert_job, matches_basic_filters
from src.ingestors.url_resolver import register_discovered_board
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
            # Fallback ONLY when explicit jobboards.search_terms is not configured
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
                logger.debug("Could not read master_profile for search terms: %s", e)

    # 4. Locations: prioritize explicit config.jobboards.locations
    locs = list(locations) if locations else []
    if not locs:
        if jb_cfg and jb_cfg.locations:
            locs.extend(jb_cfg.locations)
        else:
            # Fallback ONLY when explicit jobboards.locations is not configured
            try:
                profile = get_master_profile()
                if profile:
                    loc = profile.get("location")
                    if loc and loc not in locs:
                        locs.append(loc)
                    app_defaults = profile.get("application_defaults") or {}
                    city = app_defaults.get("current_city")
                    if city and city not in locs:
                        locs.append(city)
            except Exception as e:
                logger.debug("Could not read master_profile for locations: %s", e)

            if not locs and cfg.candidate_preferences and cfg.candidate_preferences.allowed_locations:
                for al in cfg.candidate_preferences.allowed_locations:
                    al_clean = al.strip().title()
                    if al_clean not in locs:
                        locs.append(al_clean)

    # Deduplicate preserving order
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


def _scrape_sync(
    site: str | list[str] | None = None,
    sites: list[str] | None = None,
    search_term: str = "",
    location: str = "",
    results_wanted: int = 20,
    hours_old: int = 72,
    **kwargs,
) -> pd.DataFrame:
    target = sites or (site if isinstance(site, list) else ([site] if site else ["naukri", "linkedin", "indeed"]))
    try:
        df = scrape_jobs(
            site_name=target,
            search_term=search_term,
            location=location,
            results_wanted=results_wanted,
            hours_old=hours_old,
            country_indeed="india",
            linkedin_fetch_description=True,
            verbose=0,
        )
        return df if df is not None and not df.empty else pd.DataFrame()
    except Exception as e:
        logger.warning(
            "JobSpy scrape failed for sites=%s, term='%s', loc='%s': %s",
            target,
            search_term,
            location,
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
    terms, locs, target_sites, res_count, hrs = get_jobboards_parameters(
        search_terms=search_terms,
        locations=locations,
        sites=sites,
        results_wanted=results_wanted,
        hours_old=hours_old,
    )

    combos = [(term, loc) for term in terms for loc in locs]
    total_combos = len(combos)
    sites_label = ", ".join(target_sites)
    print(
        f"  📋 [JobSpy Plan] {total_combos} queries across [{sites_label}] ({len(terms)} terms × {len(locs)} locations)",
        flush=True,
    )

    total_added = 0
    for idx, (term, loc) in enumerate(combos, 1):
        print(
            f"  🔍 [JobSpy {idx}/{total_combos}] Querying '{term}' in '{loc}' on [{sites_label}]...",
            flush=True,
        )
        try:
            df = await asyncio.to_thread(
                _scrape_sync,
                sites=target_sites,
                search_term=term,
                location=loc,
                results_wanted=res_count,
                hours_old=hrs,
            )
            if df is None or df.empty:
                print(f"    ↳ No postings returned for '{term}' in '{loc}'.", flush=True)
                continue

            site_added = 0
            site_counts = {}
            for _, row in df.iterrows():
                title = _get_row_str(row, "title")
                company = _get_row_str(row, "company")
                location_val = _get_row_str(row, "location")
                description = _get_row_str(row, "description")
                job_url = _get_row_str(row, "job_url")
                job_url_direct = _get_row_str(row, "job_url_direct")
                row_site = _get_row_str(row, "site") or "jobboard"

                site_counts[row_site] = site_counts.get(row_site, 0) + 1

                apply_url = job_url_direct or job_url
                if not title or not apply_url:
                    continue

                register_discovered_board(apply_url)

                if matches_basic_filters(title, location_val):
                    inserted = insert_job(
                        None,
                        f"jobspy_{row_site}",
                        company or "Unknown",
                        title,
                        location_val or loc,
                        apply_url,
                        clean_html(description),
                    )
                    if inserted:
                        site_added += 1

            total_added += site_added
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
