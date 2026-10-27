import httpx
import pytest
import respx

from src.ingestors.gmail_ingestor import (
    extract_job_links_from_email_html,
    fetch_linkedin_guest_job,
    poll_gmail_alerts,
)


def test_extract_job_links_from_email_html():
    sample_email_html = """
    <html>
      <body>
        <h1>New Jobs Recommended for You</h1>
        <p>Check out this role at Microsoft:</p>
        <a href="https://www.linkedin.com/comm/jobs/view/4099887766?midToken=abc&trk=eml-job-alert">
          Machine Learning Engineer
        </a>
        <br/>
        <a href="https://www.linkedin.com/jobs/view/4099887766">Duplicate Link</a>
        <br/>
        <p>Another opening at Stripe:</p>
        <a href="https://boards.greenhouse.io/stripe/jobs/55443322?gh_jid=55443322">
          Staff AI Engineer
        </a>
      </body>
    </html>
    """
    res = extract_job_links_from_email_html(sample_email_html)
    assert res["linkedin_job_ids"] == ["4099887766"]
    assert "https://boards.greenhouse.io/stripe/jobs/55443322" in res["direct_ats_urls"]


@pytest.mark.asyncio
async def test_fetch_linkedin_guest_job_success():
    job_id = "4099887766"
    mock_guest_html = """
    <div class="topcard">
      <h1 class="top-card-layout__title">Senior Computer Vision Engineer</h1>
      <a class="topcard__org-name-link">Autonomous Tech</a>
      <span class="topcard__flavor topcard__flavor--bullet">Hyderabad, Telangana, India</span>
      <div class="show-more-less-html__markup">
        <p>Looking for PyTorch and CUDA experts with 3+ years experience.</p>
      </div>
      <a class="apply-button" href="https://www.linkedin.com/jobs/view/externalApply/123?url=https%3A%2F%2Fjobs.lever.co%2Fautonomoustech%2F7788">
        Apply on company site
      </a>
    </div>
    """
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}").respond(
                status_code=200, html=mock_guest_html
            )
            data = await fetch_linkedin_guest_job(client, job_id)
            assert data is not None
            assert data["title"] == "Senior Computer Vision Engineer"
            assert data["company"] == "Autonomous Tech"
            assert "Hyderabad" in data["location"]
            assert "PyTorch and CUDA" in data["jd_text"]
            assert data["external_apply_url"] == "https://jobs.lever.co/autonomoustech/7788"


@pytest.mark.asyncio
async def test_poll_gmail_alerts_no_credentials(monkeypatch):
    monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    count = await poll_gmail_alerts()
    assert count == 0


@pytest.mark.asyncio
async def test_poll_gmail_alerts_flow(monkeypatch):
    monkeypatch.setenv("GMAIL_ADDRESS", "test@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "secret-app-password")

    mock_emails = [
        {
            "message_id": "<alert-123@linkedin.com>",
            "subject": "3 new jobs for Machine Learning Engineer in Hyderabad",
            "sender": "jobalerts-noreply@linkedin.com",
            "html": '<a href="https://www.linkedin.com/comm/jobs/view/987654321">ML Role</a>',
        }
    ]

    mock_job_data = {
        "job_id": "987654321",
        "title": "AI / ML Engineer",
        "company": "DeepVision Corp",
        "location": "Hyderabad, India",
        "jd_text": "Deep learning pipelines with Python and PyTorch.",
        "external_apply_url": "https://jobs.ashbyhq.com/deepvision/job-123",
    }

    inserted_records = []
    processed_records = []

    async def mock_fetch(client, job_id):
        return mock_job_data

    captured_sync_kwargs = {}

    def mock_fetch_sync(**kwargs):
        captured_sync_kwargs.update(kwargs)
        return mock_emails

    monkeypatch.setattr(
        "src.ingestors.gmail_ingestor.fetch_alert_emails_sync",
        mock_fetch_sync,
    )
    monkeypatch.setattr(
        "src.ingestors.gmail_ingestor.fetch_linkedin_guest_job",
        mock_fetch,
    )
    monkeypatch.setattr(
        "src.ingestors.gmail_ingestor.insert_job",
        lambda *args, **kwargs: inserted_records.append((args, kwargs)) or True,
    )
    monkeypatch.setattr(
        "src.ingestors.gmail_ingestor.mark_email_processed",
        lambda msg_id, sub, snd, jobs: processed_records.append(msg_id) or True,
    )

    count = await poll_gmail_alerts()
    assert count == 1
    assert len(inserted_records) == 1
    # Check that source_ats is gmail_alert and company is DeepVision Corp
    assert inserted_records[0][0][1] == "gmail_alert"
    assert inserted_records[0][0][2] == "DeepVision Corp"
    assert inserted_records[0][1]["original_source_url"] == "https://www.linkedin.com/jobs/view/987654321"
    assert "<alert-123@linkedin.com>" in processed_records
    assert captured_sync_kwargs.get("gmail_label") == "professional-job_alerts"



def test_find_imap_folder_matching():
    from unittest.mock import MagicMock
    from src.ingestors.gmail_ingestor import _find_imap_folder

    mock_mail = MagicMock()
    mock_mail.list.return_value = (
        "OK",
        [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasChildren) "/" "Professional"',
            b'(\\HasNoChildren) "/" "Professional/Job_Alerts"',
        ],
    )
    assert _find_imap_folder(mock_mail, "professional-job_alerts") == "Professional/Job_Alerts"
    assert _find_imap_folder(mock_mail, "Professional/Job_Alerts") == "Professional/Job_Alerts"
    assert _find_imap_folder(mock_mail, "non-existent") is None


def test_fetch_alert_emails_sync_strict_label_enforcement(monkeypatch):
    import imaplib
    from unittest.mock import MagicMock
    from src.ingestors.gmail_ingestor import fetch_alert_emails_sync

    mock_mail = MagicMock()
    # Let select fail or return empty
    mock_mail.select.return_value = ("NO", [b"Mailbox not found"])
    mock_mail.list.return_value = ("OK", [b'(\\HasNoChildren) "/" "INBOX"'])
    mock_mail.uid.return_value = ("OK", [b""])

    monkeypatch.setattr(imaplib, "IMAP4_SSL", lambda host, port: mock_mail)

    emails = fetch_alert_emails_sync(
        "test@example.com",
        "secret",
        lookback_days=3,
        gmail_label="professional-job_alerts",
    )
    # Must immediately return empty list and NOT fall back to INBOX
    assert emails == []
