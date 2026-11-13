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
                    "UADE_URL", "UADE_SHA256", "BENCODETOOLS_REF", "LIBZAKALWE_REF",
                    "LIBGME_URL", "LIBGME_SHA256", "LIBOPENMPT_VERSION", "LIBOPENMPT_SHA256",
                    "LIBKSS_COMMIT", "HT_COMMIT", "PSFLIB_COMMIT", "MILKYTRACKER_COMMIT",
                    "DEVILBOX_COMMIT", "PORTABLE_MDX_COMMIT", "KSS_DRIVERS_COMMIT",
                    "MGSDRV_H_SHA256", "GOATTRACKER_VERSION", "GOATTRACKER_SHA256",
                    "SUNVOX_LIB_VERSION", "SUNVOX_LIB_SHA256", "FURNACE_COMMIT",
                    "LIBXMP_VERSION", "LIBXMP_SHA256"}
    # (the SID chain's pins are read in a loop: ``arg "${key}_VERSION"``)
    assert 'for c in libresidfp:RESIDFP libsidplayfp:LIBSIDPLAYFP sidplayfp:SIDPLAYFP' in COLLECTOR
    used |= {f"{k}_{f}" for k in ("RESIDFP", "LIBSIDPLAYFP", "SIDPLAYFP") for f in ("VERSION", "SHA256")}
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
    # kssrender builds the libkss commit the collector bundles (build.sh's own
    # default — for a build without the Dockerfile — is the same commit).
    assert 'LIBKSS_COMMIT="$LIBKSS_COMMIT"' in DOCKERFILE
    kss_default = re.search(r'LIBKSS_COMMIT="\$\{LIBKSS_COMMIT:-(\w+)\}"',
                            (ROOT / "soniqboom/native/kssrender/build.sh").read_text())
    assert kss_default and kss_default.group(1) == _docker_args()["LIBKSS_COMMIT"]
    # dsfrender likewise builds the Highly Theoretical and psflib commits the
    # collector bundles.
    assert 'HT_COMMIT="$HT_COMMIT" PSFLIB_COMMIT="$PSFLIB_COMMIT"' in DOCKERFILE
    dsf_build = (ROOT / "soniqboom/native/dsfrender/build.sh").read_text()
    for name in ("HT_COMMIT", "PSFLIB_COMMIT"):
        default = re.search(r'%s="\$\{%s:-(\w+)\}"' % (name, name), dsf_build)
        assert default and default.group(1) == _docker_args()[name], name
    # milkyrender likewise builds the MilkyTracker commit the collector bundles.
    assert 'MILKYTRACKER_COMMIT="$MILKYTRACKER_COMMIT"' in DOCKERFILE
    milky_default = re.search(r'MILKYTRACKER_COMMIT="\$\{MILKYTRACKER_COMMIT:-(\w+)\}"',
                              (ROOT / "soniqboom/native/milkyrender/build.sh").read_text())
    assert milky_default and milky_default.group(1) == _docker_args()["MILKYTRACKER_COMMIT"]
    # mlrender likewise builds the DEViLBOX commit the collector bundles.
    assert 'DEVILBOX_COMMIT="$DEVILBOX_COMMIT"' in DOCKERFILE
    ml_default = re.search(r'DEVILBOX_COMMIT="\$\{DEVILBOX_COMMIT:-(\w+)\}"',
                           (ROOT / "soniqboom/native/mlrender/build.sh").read_text())
    assert ml_default and ml_default.group(1) == _docker_args()["DEVILBOX_COMMIT"]
    # mdxrender likewise builds the portable_mdx commit the collector bundles.
    assert 'PORTABLE_MDX_COMMIT="$PORTABLE_MDX_COMMIT"' in DOCKERFILE
    mdx_default = re.search(r'PORTABLE_MDX_COMMIT="\$\{PORTABLE_MDX_COMMIT:-(\w+)\}"',
                            (ROOT / "soniqboom/native/mdxrender/build.sh").read_text())
    assert mdx_default and mdx_default.group(1) == _docker_args()["PORTABLE_MDX_COMMIT"]
    assert '"https://github.com/yosshin4004/portable_mdx/archive/${PORTABLE_MDX_COMMIT}.tar.gz"' in COLLECTOR
    # kssrender's MGSDRV header, gt2reloc, sunvoxrender and furnace build what
    # the collector bundles (each build.sh's own default the same pin).
    native = ROOT / "soniqboom/native"
    for name, build in (("KSS_DRIVERS_COMMIT", "kssrender"), ("MGSDRV_H_SHA256", "kssrender"),
                        ("GOATTRACKER_VERSION", "goattracker"), ("GOATTRACKER_SHA256", "goattracker"),
                        ("SUNVOX_LIB_VERSION", "sunvoxrender"), ("SUNVOX_LIB_SHA256", "sunvoxrender"),
                        ("FURNACE_COMMIT", "furnace"),
                        ("LIBXMP_VERSION", "xmprender"), ("LIBXMP_SHA256", "xmprender")):
        assert f'{name}="${name}"' in DOCKERFILE, name
        default = re.search(r'%s="\$\{%s:-([\w.]+)\}"' % (name, name),
                            (native / build / "build.sh").read_text())
        assert default and default.group(1) == _docker_args()[name], name
    # A missing tag must fail the step, not silently build the default branch.
    assert 'git checkout -q "$BENCODETOOLS_REF" \\' in DOCKERFILE
    assert 'git checkout -q "$LIBZAKALWE_REF" &&' in DOCKERFILE
    # The SID chain builds what it pins, from the URL the source collector fetches.
    for key, name in (("RESIDFP", "libresidfp"), ("LIBSIDPLAYFP", "libsidplayfp"),
                      ("SIDPLAYFP", "sidplayfp")):
        assert f'sid_src {name} "${key}_VERSION" "${key}_SHA256"' in DOCKERFILE, name
    assert ('"https://github.com/libsidplayfp/$1/releases/download/v$2/$1-$2.tar.gz"'
            in DOCKERFILE)
    assert ('"https://github.com/libsidplayfp/${name}/releases/download/v${ver}/${name}-${ver}.tar.gz"'
            in COLLECTOR)
    # …and links the reSIDfp engine (without it every SID would fail to render).
    assert "readelf -d /out/lib/libsidplayfp.so" in DOCKERFILE
    # libopenmpt builds the release tarball the collector fetches.
    url = "https://lib.openmpt.org/files/libopenmpt/src/libopenmpt-${%s}+release.autotools.tar.gz"
    assert '"' + url % "LIBOPENMPT_VERSION" + '"' in DOCKERFILE
    assert '"' + url % "OMPTVER" + '"' in COLLECTOR
    assert "COPY --from=openmpt /out/ /out/" in DOCKERFILE
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
    for name in ("ZXTUNE_SHA256_X86_64", "ZXTUNE_SHA256_ARM64", "SC68_SHA256", "UADE_SHA256",
                 "LIBGME_SHA256", "RESIDFP_SHA256", "LIBSIDPLAYFP_SHA256", "SIDPLAYFP_SHA256",
                 "LIBOPENMPT_SHA256", "MGSDRV_H_SHA256", "GOATTRACKER_SHA256", "SUNVOX_LIB_SHA256",
                 "LIBXMP_SHA256"):
        assert re.search(rf"^ARG {name}=[0-9a-f]{{64}}$", DOCKERFILE, re.M), name
    for f in ("/tmp/zx.tar.gz", "/tmp/sc68.tar.gz", "/tmp/uade.tar.bz2", "/tmp/libgme.tar.gz",
              "/tmp/libopenmpt.tar.gz"):
        assert f'  {f}" | sha256sum -c -' in DOCKERFILE, f
    assert 'echo "$3  /tmp/$1.tar.gz" | sha256sum -c -' in DOCKERFILE      # the SID chain


def test_image_carries_third_party_licence_texts():
    doc = "/out/share/doc/third-party"
    gate = DOCKERFILE.split("ARG ALLOW_MISSING_RENDERERS")[1]
    for path in ("zxtune/LICENSE.md", "zxtune/3rdparty/unrar/license.txt",
                 "psgplay/licence/GPL-2.0", "psgplay/lib/toslibc/licence/GPL-2.0",
                 "sc68/COPYING", "uade/COPYING", "uade/COPYING.GPL",
                 "bencodetools/LICENSE", "libzakalwe/COPYING", "libgme/license.txt",
                 "libresidfp/COPYING", "libsidplayfp/COPYING", "sidplayfp/COPYING",
                 "libsidplayfp/hashlib-LICENSE", "sidplayfp/ghc-filesystem-LICENSE",
                 "libopenmpt/LICENSE", "libopenmpt/LICENSE.BSD-3-Clause.txt",
                 "libopenmpt/LICENSE.BSL-1.0.txt", "libopenmpt/TinyFFT-NOTICE",
                 "libkss/MGSDRV-NOTICE.txt", "goattracker/COPYING", "sunvox/LICENSE.txt",
                 "sunvox/lib_sunvox-LICENSE", "furnace/LICENSE", "libxmp/libxmp-LICENSE",
                 "sawteethrender/NostalgicPlayer-LICENSE"):
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
    np_lic = (ROOT / "soniqboom" / "native" / "sawteethrender" / "NostalgicPlayer-LICENSE").read_text()
    assert "MIT" in np_lic and "Thomas Neumann" in np_lic
    assert "NostalgicPlayer-LICENSE" in (ROOT / "soniqboom" / "native" / "sawteethrender"
                                         / "sawteethrender.c").read_text()


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


def test_debian_sources_come_from_the_images_release():
    # The Debian source packages are collected in a container of the same
    # release as the image's base — a base-image bump must move both.
    bases = set(re.findall(r"^FROM python:[\d.]+-slim-(\w+)", DOCKERFILE, re.M))
    assert len(bases) == 1, bases
    release = bases.pop()
    assert f"-w /w debian:{release} \\" in RELEASE
    debsrc = (ROOT / "scripts" / "collect_debian_sources.sh").read_text()
    assert f"debian:{release}" in debsrc


def test_the_source_bundle_and_builds_leave_out_unlicensed_parts():
    """Furnace's demos, instruments and wavetables state no licence: the
    source bundle drops them (a demo is only the builder's render check,
    never installed).  Of kss-drivers only MGSDRV's header is fetched —
    hash-checked — by the build and the bundle; libkss's kss-drivers
    submodule is never initialised and the other drivers are compiled out."""
    assert re.search(r'rm -rf "\$\{FURNACE_DIR:\?\}/demos" "\$\{FURNACE_DIR:\?\}/instruments" '
                     r'"\$\{FURNACE_DIR:\?\}/wavetables"', COLLECTOR)
    assert "submodule update -q --init --depth 1 extern/fmt extern/adpcm" in COLLECTOR
    kss_build = (ROOT / "soniqboom" / "native" / "kssrender" / "build.sh").read_text()
    for text in (COLLECTOR, kss_build):
        assert "kss-drivers/" in text and "/mgsdrv.h" in text
        assert "modules/kss-drivers" not in text and "submodule update --init --recursive" not in text
    assert "MGSDRV_H_SHA256" in COLLECTOR and '!= "$MGSDRV_H_SHA256"' in kss_build
    for drv in ("KINROU", "MBR143", "MPK106", "OPX4KSS"):
        assert f"-DEXCLUDE_DRIVER_{drv}" in kss_build, drv
    furnace_build = (ROOT / "soniqboom" / "native" / "furnace" / "build.sh").read_text()
    assert 'cp "$src/demos/' in furnace_build.split('if [ "${SELFTEST_DIR:-}" ]')[1]
    assert "COPY --from=builder /selftest" not in DOCKERFILE and "/selftest " not in \
        DOCKERFILE.split("FROM python:3.12-slim-trixie\n")[-1]
