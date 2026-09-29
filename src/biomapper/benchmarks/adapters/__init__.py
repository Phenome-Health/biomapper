"""Dataset adapters: source bytes -> (mapper-ready input_df, dataset_card).

Every adapter isolates network access so its transform is unit-testable on an in-memory fixture,
and every adapter carries the held-out gold columns alongside the query without ever handing them
to the mapper.
"""
