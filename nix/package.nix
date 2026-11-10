{
  lib,
  stdenv,
  makeWrapper,
  gnuradio,
  qt6,
  libpulseaudio,
  soapysdr,
  soapyrtlsdr,
  openssh,
  gsettings-desktop-schemas,
  gtk3,
  adwaita-icon-theme,
  hicolor-icon-theme,
  shared-mime-info,
  version ? "0.1.0",
  applicationSrc ? lib.cleanSource ../.,
}:

let
  python = gnuradio.passthru.python;
  pythonPackages =
    ps:
    gnuradio.passthru.pythonPkgs
    ++ [
      ps.pyqt6
      ps.scipy
      ps.soapysdr
    ];
  pythonEnv = python.withPackages pythonPackages;
  checkPythonEnv = python.withPackages (ps: (pythonPackages ps) ++ [ ps.pytest ]);
  sitePackages = "${placeholder "out"}/${python.sitePackages}";
  soapyPluginPath = lib.makeSearchPath soapysdr.passthru.searchPath [ soapyrtlsdr ];
  # GLib's native GTK file chooser reads GSETTINGS_SCHEMA_DIR. Nix schema
  # packages install compiled schemas under this versioned directory.
  # GSETTINGS_SCHEMAS_PATH is retained for tools that use that spelling.
  settingsSchemaPath = lib.concatMapStringsSep ":" (
    package:
    "${package}/share/gsettings-schemas/"
    + "${package.pname}-${package.version}/glib-2.0/schemas"
  ) [
    gsettings-desktop-schemas
    gtk3
  ];
  xdgDataPath = lib.makeSearchPath "share" [
    gsettings-desktop-schemas
    gtk3
    adwaita-icon-theme
    hicolor-icon-theme
    shared-mime-info
  ];
in
stdenv.mkDerivation {
  pname = "deviation-station";
  version = version;
  src = applicationSrc;

  nativeBuildInputs = [
    makeWrapper
    qt6.wrapQtAppsHook
  ];

  buildInputs = [
    pythonEnv
    qt6.qtbase
    qt6.qtmultimedia
    libpulseaudio
    openssh
    gsettings-desktop-schemas
    gtk3
    adwaita-icon-theme
    hicolor-icon-theme
    shared-mime-info
  ];

  dontBuild = true;
  doCheck = true;
  nativeCheckInputs = [ checkPythonEnv ];

  checkPhase = ''
    runHook preCheck
    export HOME="$(mktemp -d)"
    export PYTHONPATH="${applicationSrc}/src"
    ${checkPythonEnv}/bin/python -m pytest -q -p no:cacheprovider "${applicationSrc}/tests"
    runHook postCheck
  '';

  installPhase = ''
    runHook preInstall

    mkdir -p "${sitePackages}"
    cp -r "${applicationSrc}/src/deviation_meter" "${sitePackages}/deviation_meter"

    mkdir -p "$out/bin"
    makeWrapper "${pythonEnv}/bin/python3" "$out/bin/devstation" \
      --add-flags "-m deviation_meter" \
      --prefix PYTHONPATH : "${sitePackages}" \
      --prefix SOAPY_SDR_PLUGIN_PATH : "${soapyPluginPath}" \
      --prefix PATH : "${openssh}/bin" \
      --prefix LD_LIBRARY_PATH : "${lib.makeLibraryPath [ libpulseaudio ]}" \
      --prefix GSETTINGS_SCHEMA_DIR : "${settingsSchemaPath}" \
      --prefix GSETTINGS_SCHEMAS_PATH : "${settingsSchemaPath}" \
      --prefix XDG_DATA_DIRS : "${xdgDataPath}"

    # Keep the old executable name working for existing scripts.
    ln -s devstation "$out/bin/deviation-meter"

    install -Dm644 "${applicationSrc}/README.md" "$out/share/doc/deviation-station/README.md"
    install -Dm644 "${applicationSrc}/INSTALL.md" "$out/share/doc/deviation-station/INSTALL.md"
    install -Dm644 "${applicationSrc}/PROJECT_PLAN.md" "$out/share/doc/deviation-station/PROJECT_PLAN.md"
    install -Dm644 "${applicationSrc}/nix/deviationstation.desktop" \
      "$out/share/applications/deviationstation.desktop"

    runHook postInstall
  '';

  meta = with lib; {
    description = "A GNU Radio FM deviation instrument for bench work, repeaters, and sound-card modem calibration.";
    longDescription = ''
      DeviationStation measures FM deviation using Bessel carrier nulls and
      discriminator-based digital waveform analysis. Intended for amateur-radio
      bench work, repeater adjustment, MMDVM/Module17 setup, and sound-card
      modem calibration.
    '';
    homepage = "https://github.com/ben-kuhn/deviation-station";
    platforms = platforms.linux;
    mainProgram = "devstation";
  };
}
