from antidetect.launch_verification import LaunchVerification, verification_origin
from antidetect.loopback import open_loopback
from antidetect.models import FingerprintConfig
from antidetect.fingerprint import generate_fingerprint


def test_timeout_is_terminal_and_never_allows_navigation():
    result = LaunchVerification("A")
    assert not result.wait(0)
    assert result.state == "timeout"
    assert not result.complete("A", True)
    assert not result.wait(0)


def test_other_generation_cannot_verify_current_launch():
    result = LaunchVerification("B")
    assert not result.complete("A", True)
    assert result.state == "pending"
    assert result.complete("B", False)
    assert not result.wait(0)


def test_only_successful_result_allows_navigation():
    result = LaunchVerification("A")
    assert result.complete("A", True)
    assert result.wait(0)
    assert not result.complete("A", False)


def test_origin_serves_static_no_store_document():
    with verification_origin() as url:
        with open_loopback(url, timeout=2) as response:
            assert response.headers["Cache-Control"] == "no-store"
            assert b"Browser configuration check" in response.read()


def test_legacy_revision_is_read_compatibly_new_identity_explicitly_opts_in():
    old = FingerprintConfig.model_validate({"canvas_noise_seed": 98765})
    assert old.fingerprint_algorithm_version == 1
    assert old.verification_mode == "compatibility"
    assert old.canvas_noise_seed == 98765
    new = generate_fingerprint()
    assert new.fingerprint_algorithm_version == 2
    assert new.verification_mode == "strict"
