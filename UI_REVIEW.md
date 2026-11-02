# UI review — November 2, 2026

Implemented a visual and interaction pass across Policy Studio and the Control Room.

## Policy Studio

- Collapsible Noul, Choice and Score editors reduce the long-form scroll.
- Clear immutable-view versus draft-edit status and next-step guidance.
- Larger, consistently stacked fields and readable disabled values.
- Baseline/draft outcome cards and selected-row styling; operator-facing verdicts use Allow, Review and Deny.
- Draft details appear first; baseline details and raw requests remain expandable.
- Recorded experiment IDs and baseline/draft identities prevent confusing old results with current selectors.
- Editing invalidates visible results and hides stale export/preview actions.
- Unsaved edits are protected when cloning or switching drafts.
- Busy operations disable configuration controls; Enter in the editor does not reload the page.
- Improved synthetic JSON validation and synchronization of source type.
- Phone navigation jumps directly to Configure, Compare and Inspect.

## Control Room

- Mobile action buttons wrap into a usable grid.
- Dialogs fit the viewport and scroll; invoice checkboxes retain their intended width.
- Improved keyboard focus, long policy ID wrapping and compact-screen empty-state sizing.
- Improved small labels and evidence text legibility.

## Verification

Browser checked Policy Studio at 390px, 900px and desktop width; no horizontal document overflow at the tested phone/tablet sizes. Inspected desktop and phone Control Room, new-mandate dialog and Policy Lab. Verified draft creation, name editing and saving, saved comparison loading, selected scenario inspection, typed editor sections, and mobile scenario navigation. Studio console returned no warning/error entries. Both JavaScript files passed `node --check`.

No new model evaluations were needed for this UI pass; comparison inspection used the stored live Jev experiment. A nonactivated `UI review draft` remains available in draft history. Financial authorization behavior was not changed.

Screenshots can be captured from the running local demo.
