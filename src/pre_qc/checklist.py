"""Good/bad review criteria, shown as a reference panel inside the app.

Plain data on purpose -- edit CHECKLIST_ITEMS directly (add/remove/reword
lines) once colleague feedback comes in. No UI for editing this in-app;
it's meant to change by editing this file and pushing an update, not by
end users.
"""

CHECKLIST_ITEMS = [
    "In focus for most of the movie (a few soft frames are fine, "
    "throughout-the-movie blur is not)",
    "Visible cells/organisms present -- not an empty or near-empty field",
    "Stage doesn't drift far enough to lose the field of view",
    "PI/fluorescence channel shows real signal, not just flat background noise",
    "No saturation/overexposure in either channel",
    "No acquisition artifacts (air bubbles, debris, focus drift onto the "
    "wrong plane, imaging outside the well)",
]
