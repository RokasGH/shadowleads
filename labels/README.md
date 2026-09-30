# labels

Analyst-maintained inputs that the pipeline reads on every run:

- `match_audit.csv`: stratified sample of place-to-company links (from `shadowleads audit-sample`). Set `verdict` to `correct` or `wrong`; wrong links are excluded from leads, and precision per method is reported.
- `dispositions.csv` (planned): analyst decisions on leads (cleared or inspected).
