Add CLI command export-orders --output PATH to this existing orders project.
Write UTF-8 CSV with columns id,total,currency, sorted by id. Empty input produces only the header.
Escape commas, quotes and newlines correctly. Keep the existing get_orders/getOrders API and status command.
Without --force, refuse to overwrite an existing file with exit code 2 and a useful error.
With --force, replace it only after the whole output has been written successfully.
Return exit code 0 on success and a nonzero code on write failure while preserving any existing file.
Add behavioral tests and README examples. Do not add runtime dependencies.
