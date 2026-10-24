import os


async def fill(page, job: dict, profile: dict) -> dict:
    fields_filled = {}
    inputs = await page.query_selector_all("input, textarea")
    for inp in inputs:
        name = (await inp.get_attribute("name") or "").lower()
        placeholder = (await inp.get_attribute("placeholder") or "").lower()
        inp_id = (await inp.get_attribute("id") or "").lower()
        inp_type = (await inp.get_attribute("type") or "text").lower()
        label = ""
        try:
            label = (
                await inp.evaluate("el => el.labels && el.labels.length ? el.labels[0].innerText : ''") or ""
            ).lower()
        except Exception:
            label = ""

        key = " ".join(filter(None, [name, placeholder, inp_id, label]))
        value = None

        if inp_type == "email" or "email" in key:
            value = profile.get("email")
        elif inp_type == "tel" or "phone" in key or "mobile" in key:
            value = profile.get("phone")
        elif "first" in key and "name" in key:
            value = profile.get("name", "").split()[0] if profile.get("name") else None
        elif "last" in key and "name" in key:
            parts = (profile.get("name") or "").split()
            value = parts[-1] if len(parts) > 1 else None
        elif "name" in key and "company" not in key and "user" not in key:
            value = profile.get("name")
        elif "linkedin" in key:
            value = profile.get("linkedin")
            if value and not value.startswith("http"):
                value = f"https://{value}"
        elif "github" in key:
            value = profile.get("github")
            if value and not value.startswith("http"):
                value = f"https://{value}"
        elif "website" in key or "portfolio" in key:
            value = profile.get("github") or profile.get("linkedin")
        elif "location" in key or "city" in key:
            value = profile.get("location")
        elif "company" in key or "organization" in key:
            exp = profile.get("experience") or []
            if exp and isinstance(exp, list) and len(exp) > 0:
                value = exp[0].get("company")

        if value and inp_type != "file":
            try:
                await inp.fill(str(value))
                fields_filled[name or placeholder or inp_id or label or "field"] = value
            except Exception:
                pass

    # Resume upload
    file_inputs = await page.query_selector_all("input[type=file]")
    pdf_path = job.get("tailored_pdf_path") or job.get("pdf_path")
    if pdf_path and os.path.exists(pdf_path):
        for fi in file_inputs:
            try:
                await fi.set_input_files(pdf_path)
                fields_filled["resume"] = pdf_path
            except Exception:
                pass

    return fields_filled
