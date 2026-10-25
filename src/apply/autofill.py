from src.apply.llm_form_agent import smart_fill_form


async def fill(page, job: dict, profile: dict) -> dict:
    """Smart LLM + DOM-distillation form filler."""
    return await smart_fill_form(page, job, profile)
