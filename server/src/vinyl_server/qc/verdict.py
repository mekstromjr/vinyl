"""Verdict logic: pass / review / recapture, plus the buy-a-commercial-copy flag.

Thresholds are deliberately loose first guesses; the qc blob records them next
to the measurements so retuning later is an evidence-based diff, not
archaeology. The split follows the house rule: recapture = redoing might fix
it; review = the DISC is the problem and a human should decide.
"""

from __future__ import annotations

from dataclasses import dataclass

from vinyl_shared.loopdetect import ROTATION_PERIOD_SEC, LockedGrooveDetector

THRESHOLDS = {
    "noise_floor_review_dbfs": -35.0,
    "clicks_review_per_min": 40.0,
    "imbalance_review_db": 6.0,
    "duration_short_recapture_sec": 90.0,
    "mono_fold_candidate_corr": 0.9,
}


@dataclass
class QCResult:
    verdict: str            # pass | review | recapture
    reasons: list[str]
    buy_commercial: bool
    report: dict


def loop_scan(envelope_100ms: list[float], speed_rpm: int, music_end_sec: float) -> dict:
    """Full-file locked-groove sweep with the SAME field-tuned detector the
    capture station runs. A trigger well before the music ends is a loop the
    live detector missed (or predates); near the end it is just the runout."""
    period = ROTATION_PERIOD_SEC.get(speed_rpm)
    if period is None:
        return {"scanned": False}
    det = LockedGrooveDetector(period)
    fired_at = None
    for i, peak in enumerate(envelope_100ms):
        if det.feed(peak, (i + 1) / 10):
            fired_at = (i + 1) / 10
            break
    mid_record = bool(fired_at is not None and fired_at < music_end_sec - 120)
    return {
        "scanned": True,
        "loop_at_sec": fired_at,
        "mid_record_loop": mid_record,
    }


def judge(
    *,
    summary: dict,
    clicks: dict,
    noise_floor_dbfs: float | None,
    dropout_starts: list[float],
    loop: dict,
    expected_runtime_sec: int | None,
    music_end_sec: float,
) -> QCResult:
    t = THRESHOLDS
    reasons: list[str] = []
    verdict = "pass"
    buy = False

    if loop.get("mid_record_loop"):
        m, s = divmod(int(loop["loop_at_sec"]), 60)
        reasons.append(f"locked groove mid-record at ~{m}:{s:02d} — clean and re-capture")
        verdict = "recapture"

    # Music materially shorter than the tracklist says: skip, mistrack, or
    # wrong side. (Captured duration is always past expected by design, so the
    # comparison is against where the music actually stopped.)
    if (
        expected_runtime_sec
        and music_end_sec < expected_runtime_sec - t["duration_short_recapture_sec"]
    ):
        reasons.append(
            f"music ends at {music_end_sec:.0f}s vs expected {expected_runtime_sec}s"
        )
        verdict = "recapture"

    if verdict != "recapture":
        if noise_floor_dbfs is not None and noise_floor_dbfs > t["noise_floor_review_dbfs"]:
            reasons.append(f"lead-in noise floor {noise_floor_dbfs} dBFS — worn/dirty disc")
            verdict = "review"
            buy = True
        if clicks["clicks_per_min"] > t["clicks_review_per_min"]:
            reasons.append(f"{clicks['clicks_per_min']} clicks/min — surface damage")
            verdict = "review"
            buy = True
        if dropout_starts:
            reasons.append(f"{len(dropout_starts)} dropout(s) in the music region")
            verdict = "review"
        if summary["channel_imbalance_db"] > t["imbalance_review_db"]:
            reasons.append(
                f"channel imbalance {summary['channel_imbalance_db']} dB — "
                "dirty stylus or channel fault"
            )
            verdict = "review"

    mono_candidate = bool(summary["phase_correlation"] >= t["mono_fold_candidate_corr"])

    report = {
        "reasons": reasons,
        **summary,
        **clicks,
        "lead_in_noise_floor_dbfs": noise_floor_dbfs,
        "dropout_starts_sec": dropout_starts,
        "loop_scan": loop,
        "music_end_sec": float(music_end_sec),
        "expected_runtime_sec": expected_runtime_sec,
        "mono_fold_candidate": mono_candidate,
        "buy_commercial_candidate": buy,
        "thresholds": t,
    }
    return QCResult(verdict=verdict, reasons=reasons, buy_commercial=buy, report=report)
