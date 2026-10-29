{
  # bazel-payloads tool pins (TIN-7477, R575, R581).
  #
  # Every job that fetches, packs, reproduces or proposes a payload runs its
  # tools through this flake, so `flake.lock` pins the Python, its zlib (the
  # only normative compressor, design C5), GnuPG for the Node SHASUMS check,
  # and pnpm 10.13.1 for `lock-update`. GNU gzip is never used to pack.
  description = "bazel-payloads: deterministic npm and Node payload tools";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
      ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAll (pkgs: {
        # The packer's interpreter: Python 3.12 and its zlib, plus PyYAML to
        # read pnpm-lock.yaml. Nothing else is on its import path.
        python = pkgs.python312.withPackages (p: [ p.pyyaml ]);

        # pnpm 10.13.1, the version gftb-site's package.json names. It runs
        # only in `lock-update`, never in fetch, pack or reproduce.
        pnpm = pkgs.stdenvNoCC.mkDerivation {
          pname = "pnpm";
          version = "10.13.1";
          src = pkgs.fetchurl {
            url = "https://registry.npmjs.org/pnpm/-/pnpm-10.13.1.tgz";
            hash = "sha512-N+vxpcejDV+r4MXfRO6NpMllygxa89urKMOhaBtwolYhjQXIHJwNz3Z+9rhVHrW5YAQrntQwDFkkIzY3fgHPrQ==";
          };
          nativeBuildInputs = [ pkgs.makeWrapper ];
          installPhase = ''
            mkdir -p $out/lib/pnpm $out/bin
            cp -r . $out/lib/pnpm
            makeWrapper ${pkgs.nodejs_22}/bin/node $out/bin/pnpm --add-flags $out/lib/pnpm/bin/pnpm.cjs
          '';
        };
      });

      devShells = forAll (pkgs: {
        default = pkgs.mkShellNoCC {
          packages = [
            self.packages.${pkgs.stdenv.hostPlatform.system}.python
            pkgs.gnupg
            pkgs.curl
            pkgs.git
          ];
          shellHook = ''
            export PYTHONDONTWRITEBYTECODE=1
          '';
        };
        lock-update = pkgs.mkShellNoCC {
          packages = [
            self.packages.${pkgs.stdenv.hostPlatform.system}.python
            self.packages.${pkgs.stdenv.hostPlatform.system}.pnpm
            pkgs.nodejs_22
          ];
        };
      });
    };
}
