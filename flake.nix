{
  description = "DeviationStation: a GNU Radio FM deviation instrument";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
      pythonEnvironment =
        pkgs:
        let
          gnuradio = pkgs.gnuradio;
        in
        gnuradio.passthru.python.withPackages (
          ps:
          gnuradio.passthru.pythonPkgs
          ++ (with ps; [
            pyqt6
            scipy
            soapysdr
            pytest
          ])
        );
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = import nixpkgs { inherit system; };
        in
        {
          default = pkgs.callPackage ./nix/package.nix { };
          deviation-station = self.packages.${system}.default;
          # Compatibility alias for existing flake consumers.
          gnuradio-deviation-meter = self.packages.${system}.default;
        }
      );

      apps = forAllSystems (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/devstation";
        };
      });

      devShells = forAllSystems (
        system:
        let
          pkgs = import nixpkgs { inherit system; };
          pythonEnv = pythonEnvironment pkgs;
        in
        {
          default = pkgs.mkShell {
            packages = [
              pythonEnv
              pkgs.gnuradio
              pkgs.soapysdr
              pkgs.soapyrtlsdr
              pkgs.hamlib
              pkgs.pulseaudio
              pkgs.qt6.qtbase
              pkgs.qt6.qtmultimedia
              pkgs.qt6.qtwayland
            ];

            shellHook = ''
              export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
              export SOAPY_SDR_PLUGIN_PATH="${
                pkgs.lib.makeSearchPath pkgs.soapysdr.passthru.searchPath [ pkgs.soapyrtlsdr ]
              }''${SOAPY_SDR_PLUGIN_PATH:+:$SOAPY_SDR_PLUGIN_PATH}"
              export QT_PLUGIN_PATH="${pkgs.qt6.qtbase}/lib/qt-6/plugins''${QT_PLUGIN_PATH:+:$QT_PLUGIN_PATH}"
              export NIXPKGS_QT6_QML_IMPORT_PATH="${
                pkgs.qt6.qtdeclarative
              }/lib/qt-6/qml''${NIXPKGS_QT6_QML_IMPORT_PATH:+:$NIXPKGS_QT6_QML_IMPORT_PATH}"
            '';
          };
        }
      );

      checks = forAllSystems (system: {
        default = self.packages.${system}.default;
      });
    };
}
