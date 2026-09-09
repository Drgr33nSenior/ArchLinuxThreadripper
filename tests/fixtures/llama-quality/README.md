# Pinned numerical CSV fixture

`ops.csv` follows `test_result::get_fields/get_values` filtered by
`csv_printer::get_fields_csv` in llama.cpp
`427291b5b34cd914a31b3fd3b61a68f6184f4b9f`:
[exact upstream source](https://github.com/ggml-org/llama.cpp/blob/427291b5b34cd914a31b3fd3b61a68f6184f4b9f/tests/test-backend-ops.cpp).

Header order, quoted fields, stringified Boolean values and empty error fields
match that printer. It does not emit `passed`. The selected test backend must
also exit successfully; an absent backend can exit zero without any test rows.

These are synthetic operation rows, not captured R9700 results. Parameter
strings exercise quoted commas but do not claim a full upstream test matrix.
Tests derive unsupported, missing, malformed and error rows from this fixture
and pair them with explicit executable-status records. No GPU qualification is
implied by passing these fixtures.

`ops-with-skips.csv` adds CPU, repeated GPU, repeated CPU and mixed skip
attribution alongside successful required operations. Pinned `test_case::eval`
joins the names of backends that reject each tensor with `, `, retaining
duplicates. The reference backend is CPU. These names identify unsupported
backends, not the selected device; supported rows still require its exact name.
The qualification integration test uses both fixtures for HIP and Vulkan on
each selected synthetic device and checks the pass/unsupported counts.
