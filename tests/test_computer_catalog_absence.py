"""Native absence evidence is distinct from the bounded visible UI catalog."""

import asyncio

import pytest

from agent.runtime.computer_protocol import ComputerResponse
from agent.runtime.macos_computer import MacComputerBackend, HelperTransportError


class CatalogTransport:
    def __init__(self, result):
        self.result = result

    async def request(self, request):
        assert request.operation == "apps"
        return ComputerResponse(request.request_id, ok=True, result=self.result)


def test_native_catalog_decodes_exact_absence_evidence_in_same_generation():
    result = {
        "catalog_generation": 9,
        "apps": [],
        "confirmed_absent_window_identity_refs": ["identity_closed_menu"],
    }
    catalog = asyncio.run(MacComputerBackend(CatalogTransport(result)).apps())
    assert catalog.generation == 9
    assert catalog.confirmed_absent_window_identity_refs == ("identity_closed_menu",)


def test_legacy_native_catalog_does_not_imply_absence():
    catalog = asyncio.run(MacComputerBackend(CatalogTransport({
        "catalog_generation": 9, "apps": [],
    })).apps())
    assert catalog.confirmed_absent_window_identity_refs == ()


@pytest.mark.parametrize("proof", [None, {}, "identity_menu", [""], ["a", "a"], [True],
                                       ["identity_" + str(i) for i in range(201)]])
def test_native_catalog_rejects_malformed_or_unbounded_absence_evidence(proof):
    result = {"catalog_generation": 9, "apps": [], "confirmed_absent_window_identity_refs": proof}
    with pytest.raises(HelperTransportError):
        asyncio.run(MacComputerBackend(CatalogTransport(result)).apps())
