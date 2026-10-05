Add CLI command export-orders --output PATH to this orders project.

Export the existing order source as UTF-8 CSV with columns id,total,currency.
Sort rows by id. An empty order list produces only the header.
Correctly escape commas, quotes and newlines in every value.

Do not overwrite an existing output without --force: return exit code 2 with a clear message and leave its contents intact.
With --force, replace the output only after writing successfully. A successful export returns 0.
On a write error, return a nonzero code and preserve any existing output file.

Keep the existing status command and public order API working.
Add behavioral tests for empty orders, ordering, escaping, overwrite refusal, forced overwrite, write failure and the existing command.
Document a working command in README. Do not add runtime dependencies.

Use the project's configured unit check and its actual executed test IDs for the plan's verification criteria.
