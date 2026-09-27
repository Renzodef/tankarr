import pytest

from tankarr.monitoring import evaluate_future_monitoring, terminal_monitor_mode


def test_completed_translation_can_still_monitor_until_final_chapter():
    manga = {
        "status": "completed",
        "last_chapter": "83",
        "preferred_language": "en",
    }

    allowed, reason = evaluate_future_monitoring(
        manga, [{"id": "chapter-80", "chapter": "80"}]
    )

    assert allowed is True
    assert "80" in reason
    assert "83" in reason


def test_completed_translation_disables_future_at_declared_final_chapter():
    manga = {
        "status": "completed",
        "last_chapter": "83",
        "preferred_language": "en",
    }

    allowed, reason = evaluate_future_monitoring(
        manga, [{"id": "chapter-83", "chapter": "83"}]
    )

    assert allowed is False
    assert "final chapter 83" in reason
    assert terminal_monitor_mode("all") == "existing"
    assert terminal_monitor_mode("future") == "none"


def test_monitoring_reason_is_provider_neutral():
    allowed, reason = evaluate_future_monitoring(
        {"status": "ongoing", "preferred_language": "en"}, []
    )
    assert allowed is True
    assert reason == "The source marks the series as ongoing."

    allowed, reason = evaluate_future_monitoring(
        {"status": "completed", "preferred_language": "en"}, []
    )
    assert allowed is False
    assert reason == (
        "The source marks the series as completed; "
        "no final chapter target is available."
    )


@pytest.mark.parametrize(
    "status",
    [
        "ended",
        "completed",
        "complete",
        "finished",
        "abandoned",
        "dropped",
        "cancelled",
        "canceled",
        "discontinued",
    ],
)
def test_every_terminal_status_disables_future_without_a_final_target(status):
    allowed, reason = evaluate_future_monitoring(
        {"status": status, "preferred_language": "en"}, []
    )

    assert allowed is False
    assert status in reason
