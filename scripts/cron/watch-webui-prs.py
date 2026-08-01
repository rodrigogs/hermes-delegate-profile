#!/usr/bin/env python3
"""Watch the hermes-webui PRs we opened upstream, and say something only when
something changed.

Silent by default: the cron job runs with --no-agent, so empty stdout means no
delivery. A daily "still open, nothing happened" message trains the reader to
ignore it, and then the one message that matters is ignored too.

State lives beside this script. First run records and stays quiet about PRs that
are simply still open; it reports only what it has not seen before.
"""
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

REPO = "nesquena/hermes-webui"
PRS = [6656, 6657, 6658, 6659]
STATE = pathlib.Path.home() / ".hermes" / "state" / "webui-pr-watch.json"
UA = "hermes-pr-watch"


def _get(path):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}{path}",
        headers={"User-Agent": UA, "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)


def fetch(number):
    return _get(f"/pulls/{number}")


def fetch_reviews(number):
    """The review verdicts. /pulls/{n} does not carry them.

    A summary-only CHANGES_REQUESTED moves neither comment counter, and
    mergeable_state goes unstable -> blocked, which is also what a missing approval
    or a failing required check looks like. Watching mergeable_state would latch on
    and produce daily noise; watching reviews reports the thing that happened.

    Measured on this repo: PR #6642 went CHANGES_REQUESTED, CHANGES_REQUESTED,
    APPROVED from one reviewer, and none of the six fields we snapshot moved for any
    of the three.
    """
    return _get(f"/pulls/{number}/reviews")


# The review states that mean a human acted. COMMENTED is a note without a verdict
# and DISMISSED is a verdict being withdrawn; both are reported, because on a PR you
# are waiting on, a maintainer saying anything at all is the event.
REVIEW_VERDICTS = {
    "CHANGES_REQUESTED": "requested changes on",
    "APPROVED": "approved",
    "COMMENTED": "commented on",
    "DISMISSED": "had a review dismissed on",
}


def snapshot(pr, reviews=()):
    """The fields worth waking someone up for."""
    # Review ids are monotonic per repo, so the highest one seen is a watermark: any
    # id above it is a review that arrived since the last run. That survives edits to
    # old reviews and does not depend on counting, which is what made comment deltas
    # unreliable.
    seen = [r for r in reviews if isinstance(r.get("id"), int)]
    latest = max((r["id"] for r in seen), default=0)
    return {
        "last_review_id": latest,
        # Kept for the message, not for detection: what the newest review actually said.
        "last_review_state": next(
            (r.get("state") for r in sorted(seen, key=lambda x: x["id"], reverse=True)),
            None,
        ),
        "last_review_by": next(
            ((r.get("user") or {}).get("login")
             for r in sorted(seen, key=lambda x: x["id"], reverse=True)),
            None,
        ),
        "state": pr.get("state"),
        "merged": bool(pr.get("merged")),
        # dirty means it stopped merging cleanly, which is on us to fix.
        "mergeable_state": pr.get("mergeable_state"),
        "review_comments": pr.get("review_comments", 0),
        "comments": pr.get("comments", 0),
        "title": pr.get("title", ""),
        "url": pr.get("html_url", ""),
    }


def describe(number, old, new):
    """What changed, in the words an operator would use. None means nothing did."""
    if old is None:
        # Only worth announcing on first sight if it is already resolved.
        if new["merged"]:
            return f"PR #{number} is already MERGED: {new['title']}"
        if new["state"] == "closed":
            return f"PR #{number} is already CLOSED unmerged: {new['title']}"
        # Open but already reviewed: the operator has not seen this either.
        if new.get("last_review_state") in ("CHANGES_REQUESTED", "APPROVED"):
            who = new.get("last_review_by") or "a reviewer"
            verb = REVIEW_VERDICTS[new["last_review_state"]]
            return f"  {who} {verb} #{number}: {new['title']}"
        return None
    lines = []
    if new["merged"] and not old["merged"]:
        lines.append(f"MERGED: #{number} {new['title']}")
    elif new["state"] == "closed" and old["state"] != "closed":
        lines.append(f"CLOSED without merging: #{number} {new['title']}")
    elif new["state"] == "open" and old["state"] == "closed":
        lines.append(f"REOPENED: #{number} {new['title']}")
    # A review that arrived since the watermark. This is the signal the comment
    # counters miss entirely: a summary-only CHANGES_REQUESTED moves neither.
    if new.get("last_review_id", 0) > old.get("last_review_id", 0):
        state = new.get("last_review_state") or "reviewed"
        who = new.get("last_review_by") or "a reviewer"
        verb = REVIEW_VERDICTS.get(state, f"left a {state} review on")
        lines.append(f"{who} {verb} #{number}: {new['title']}")
    talk = (new["review_comments"] + new["comments"]) - (old["review_comments"] + old["comments"])
    if talk > 0:
        lines.append(f"{talk} new comment(s) on #{number} — review feedback to answer")
    # dirty: the branch no longer merges cleanly. Ours to rebase.
    if new["mergeable_state"] == "dirty" and old["mergeable_state"] != "dirty":
        lines.append(f"#{number} now CONFLICTS with master — needs a rebase")
    return "\n".join(f"  {ln}" for ln in lines) if lines else None


def main():
    STATE.parent.mkdir(parents=True, exist_ok=True)
    try:
        old_all = json.loads(STATE.read_text())
    except (OSError, ValueError):
        old_all = {}

    new_all, reports, errors = {}, [], []
    for number in PRS:
        try:
            pr = fetch(number)
            # A reviews call that fails must not zero the watermark and re-announce
            # every old review on the next run, so fall back to what we already knew.
            try:
                reviews = fetch_reviews(number)
            except (urllib.error.URLError, OSError, ValueError):
                prev = old_all.get(str(number)) or {}
                reviews = [{"id": prev.get("last_review_id", 0),
                            "state": prev.get("last_review_state"),
                            "user": {"login": prev.get("last_review_by")}}]
            snap = snapshot(pr, reviews)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # A transient network failure must not look like a closed PR, and must
            # not overwrite good state with nothing.
            errors.append(f"  #{number}: could not be checked ({exc})")
            prev = old_all.get(str(number))
            if prev is not None:
                new_all[str(number)] = prev
            continue
        new_all[str(number)] = snap
        msg = describe(number, old_all.get(str(number)), snap)
        if msg:
            reports.append(msg)
            reports.append(f"    {snap['url']}")

    # Written via a temp file in the same directory and renamed: os.replace is
    # atomic, so a crash or a kill between truncate and write cannot leave a
    # half-written file behind. The state IS the watermark - a truncated one
    # re-announces every review and every status change on the next run.
    tmp = STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(new_all, indent=2) + "\n")
    os.replace(tmp, STATE)

    # A PR that could not be checked is not a resolved PR. It is missing from
    # new_all when it errored on a run with no prior state, and counting only
    # what is present would let an unreachable PR read as closed - the job
    # would announce "all resolved" and offer to delete itself while still
    # owing an answer about that PR. So a PR counts as resolved only if this
    # run actually saw it in a non-open state.
    unresolved = [
        n for n in PRS
        if str(n) not in new_all or new_all[str(n)]["state"] == "open"
    ]
    if reports:
        print("hermes-webui pull requests: something changed:")
        print("\n".join(reports))
    if errors:
        # Every failure is reported, partial or total. Staying quiet about a PR
        # that could not be reached is how a stalled watch looks like a calm one.
        print("hermes-webui PR watch could not reach GitHub:")
        print("\n".join(errors))
    if reports and not unresolved:
        print(f"\n  All {len(PRS)} are now resolved. This watch job can be removed:")
        print("    hermes cron rm webui-pr-watch")
    return 0


if __name__ == "__main__":
    sys.exit(main())
