"""Good/bad review criteria, shown as a reference panel inside the app.

Plain data on purpose -- edit CHECKLIST_ITEMS directly (add/remove/reword
lines) once colleague feedback comes in. No UI for editing this in-app;
it's meant to change by editing this file and pushing an update, not by
end users.
"""

CHECKLIST_ITEMS = [
    "In focus throughout (a few soft frames are fine; out of focus badly "
    "enough that cells can't be made out at all is not)",
    "Enough cells present to be worth analyzing -- from a truly empty "
    "field down to density too low throughout (some sparsity is fine, "
    "this is about the unusably sparse end)",
    "Stage doesn't drift far enough to lose the field of view",
    "Both BF and PI/fluorescence channels are present, and PI shows real "
    "signal -- not just flat background noise",
    "No saturation/overexposure in either channel",
    "No acquisition artifacts (air bubbles, debris, imaging outside the well)",
    "Frame count matches what was expected for this acquisition",
    "For a condition expected to kill -- check for overgrowth instead "
    "(compound likely failed or wasn't delivered)",
    "For a growth-control condition -- check that real growth actually "
    "happened (no growth may mean the well itself failed)",
]
