from rpc_state_indexer.settings import RuntimeSettings


def test_unlabelled_rpc_urls_do_not_form_distinct_provider_groups() -> None:
    settings = RuntimeSettings(RPC_URLS="https://one.invalid,https://two.invalid")

    assert [endpoint.provider_group for endpoint in settings.endpoints()] == [
        "unclassified",
        "unclassified",
    ]


def test_explicit_provider_groups_are_preserved() -> None:
    settings = RuntimeSettings(
        RPC_URLS="https://one.invalid,https://two.invalid",
        RPC_PROVIDER_GROUPS="provider-a,provider-b",
    )

    assert [endpoint.provider_group for endpoint in settings.endpoints()] == [
        "provider-a",
        "provider-b",
    ]
