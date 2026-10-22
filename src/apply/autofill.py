import os

FIELD_MAP = {
    "first name": "first_name",
    "last name": "last_name",
    "email": "email",
    "phone": "phone",
    "resume": "resume",
    "linkedin": "linkedin",
}


async def fill(page, job, profile) -> dict:
    fields_filled = {}
    # best-effort: fill inputs by label/name/placeholder
    inputs = await page.query_selector_all("input, textarea")
    for inp in inputs:
        name = (await inp.get_attribute("name") or "").lower()
        placeholder = (await inp.get_attribute("placeholder") or "").lower()
        label = ""
        # try associated label
        try:
            label = (await inp.evaluate("el => el.labels && el.labels.length ? el.labels[0].innerText : ''") or "").lower()
        except Exception:
            label = ""
        key = name or placeholder or label
        value = None
        if "email" in key:
            value = profile.get("email")
        elif "phone" in key:
            value = profile.get("phone")
        elif "first" in key and "name" in key:
            value = profile.get("name", "").split()[0] if profile.get("name") else None
        elif "last" in key and "name" in key:
            parts = (profile.get("name") or "").split()
            value = parts[-1] if len(parts) > 1 else None
        elif "linkedin" in key:
            value = profile.get("linkedin")
        if value:
            await inp.fill(str(value))
            fields_filled[key or "unknown"] = value
    # resume upload
    file_inputs = await page.query_selector_all("input[type=file]")
    for fi in file_inputs:
        pdf_path = job.get("pdf_path")
        if pdf_path and os.path.exists(pdf_path):
            await fi.set_input_files(pdf_path)
            fields_filled["resume"] = pdf_path
    return fields_filled