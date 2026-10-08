# SPDX-FileCopyrightText: 2026 S.F. Cyris
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Licence housekeeping: the release's third-party source bundle must track
the Dockerfile's pinned upstream versions, and vendored code must carry its
licence text."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text()
COLLECTOR = (ROOT / "scripts" / "collect_third_party_sources.sh").read_text()
RELEASE = (ROOT / ".github" / "workflows" / "release.yml").read_text()


def _docker_args():
    return dict(re.findall(r"^ARG (\w+)=(\S+)", DOCKERFILE, re.M))


def test_collector_reads_every_pin_from_the_dockerfile():
    used = set(re.findall(r"\$\(arg (\w+)\)", COLLECTOR))
    assert used == {"PSGPLAY_COMMIT", "ZXTUNE_REV", "SC68_URL", "SC68_SHA256",
                    "UADE_URL", "UADE_SHA256", "BENCODETOOLS_REF", "LIBZAKALWE_REF"}
    args = _docker_args()
    for name in used:
        assert name in args, f"Dockerfile lost ARG {name}"


def test_dockerfile_uses_the_pins_it_declares():
    # Each pinned version must actually drive the build, so the source bundle
    # corresponds to the binaries in the image.
    assert 'checkout "$PSGPLAY_COMMIT"' in DOCKERFILE
    assert "${ZXTUNE_REV}/linux/${ZXARCH}/zxtune_${ZXTUNE_REV}_linux_" in DOCKERFILE
    assert '"$SC68_URL"' in DOCKERFILE
    assert '"$UADE_URL"' in DOCKERFILE
    # A missing tag must fail the step, not silently build the default branch.
    assert 'git checkout -q "$BENCODETOOLS_REF" \\' in DOCKERFILE
    assert 'git checkout -q "$LIBZAKALWE_REF" &&' in DOCKERFILE
    assert not re.search(r"git checkout[^\n]*\|\| true", DOCKERFILE)


def test_release_attaches_sources_and_debian_manifest():
    assert "scripts/collect_third_party_sources.sh" in RELEASE
    assert 'NAME="soniqboom-${VERSION}-third-party-sources"' in RELEASE
    assert 'gh release upload "${GITHUB_REF_NAME}" "${NAME}.tar"' in RELEASE
    assert "dpkg-query" in RELEASE and "debian-packages-" in RELEASE
    assert "needs: [release, docker-build]" in RELEASE
    # The image tags go out only after every source asset is attached.
    assert "needs: [docker-build, release-manifests]" in RELEASE
    assert "scripts/collect_debian_sources.sh" in RELEASE
    assert '"${NAME}.tar" "${NAME}.txt"' in RELEASE


def test_downloads_are_hash_checked():
    for name in ("ZXTUNE_SHA256_X86_64", "ZXTUNE_SHA256_ARM64", "SC68_SHA256", "UADE_SHA256"):
        assert re.search(rf"^ARG {name}=[0-9a-f]{{64}}$", DOCKERFILE, re.M), name
    for f in ("/tmp/zx.tar.gz", "/tmp/sc68.tar.gz", "/tmp/uade.tar.bz2"):
        assert f'  {f}" | sha256sum -c -' in DOCKERFILE, f


def test_image_carries_third_party_licence_texts():
    doc = "/out/share/doc/third-party"
    gate = DOCKERFILE.split("ARG ALLOW_MISSING_RENDERERS")[1]
    for path in ("zxtune/LICENSE.md", "zxtune/3rdparty/unrar/license.txt",
                 "psgplay/licence/GPL-2.0", "psgplay/lib/toslibc/licence/GPL-2.0",
                 "sc68/COPYING", "uade/COPYING", "uade/COPYING.GPL",
                 "bencodetools/LICENSE", "libzakalwe/COPYING"):
        assert f"{doc}/{path}" in gate, path
    assert "COPY --from=builder /out/share/                /usr/local/share/" in DOCKERFILE
    ignored = {l.strip() for l in (ROOT / ".dockerignore").read_text().splitlines()
               if l.strip() and not l.startswith("#")}
    for keep in ("LICENSE", "THIRD-PARTY-LICENSES.md", "soniqboom", "soniqboom/native"):
        assert keep not in ignored, keep


def test_vendored_code_ships_its_licence():
    hvl = (ROOT / "soniqboom" / "native" / "hvl" / "LICENSE").read_text()
    assert "BSD 3-Clause" in hvl and "Pete Gordon" in hvl
    assert (ROOT / "soniqboom" / "native" / "stsound" / "LICENSE").is_file()


def test_sid_wasm_corresponds_to_its_committed_source():
    import hashlib
    src = ROOT / "soniqboom" / "native" / "sidwasm"
    build = (src / "build.sh").read_text()
    assert re.search(r'^LIBSIDPLAYFP_URL="https://\S+libsidplayfp-[\d.]+\.tar\.gz"$', build, re.M)
    assert re.search(r'^LIBSIDPLAYFP_SHA256="[0-9a-f]{64}"$', build, re.M)
    assert "sha256sum -c" in build
    for f in ("COPYING", "sidwasm.cpp", "README.md"):
        assert (src / f).is_file()
    assert sorted(p.name for p in (src / "patches").glob("*.patch"))
    # The vendored binaries must be the ones the README records for this source.
    # Every file the patches touch carries a dated modification notice.
    for patch in (src / "patches").glob("*.patch"):
        text = patch.read_text()
        files = re.findall(r"^\+\+\+ b/(\S+)", text, re.M)
        assert files
        assert text.count("+ * Modified by S.F. Cyris for SoniqBoom, 2026-10-07:") == len(files), patch.name
    assert (vendor := ROOT / "soniqboom" / "frontend" / "js" / "vendor")
    assert (vendor / "sidwasm.js").read_text().startswith((src / "banner.js").read_text())
    readme = (src / "README.md").read_text()
    vendor = ROOT / "soniqboom" / "frontend" / "js" / "vendor"
    for name in ("sidwasm.js", "sidwasm.wasm"):
        digest = hashlib.sha256((vendor / name).read_bytes()).hexdigest()
        assert f"{digest}  {name}" in readme, f"{name} differs from the build recorded in sidwasm/README.md"
