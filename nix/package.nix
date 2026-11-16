{ lib, python3Packages, playwright-driver, makeFontsConf, dejavu_fonts, liberation_ttf }:
let
  browsers = playwright-driver.browsers.override {
    withChromium = false;
    withChromiumHeadlessShell = true;
    withFirefox = false;
    withWebkit = false;
  };
  fonts = makeFontsConf { fontDirectories = [ dejavu_fonts liberation_ttf ]; };
in
python3Packages.buildPythonApplication {
  pname = "canvas-api";
  version = "0.1.0";
  pyproject = true;
  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [ ../pyproject.toml ../canvas_api ../tests ];
  };

  build-system = [ python3Packages.setuptools ];
  dependencies = with python3Packages; [ fastapi uvicorn playwright pyotp ];
  nativeCheckInputs = [ python3Packages.pytestCheckHook python3Packages.httpx ];

  makeWrapperArgs = [
    "--set-default PLAYWRIGHT_BROWSERS_PATH ${browsers}"
    "--set-default PLAYWRIGHT_SKIP_VALIDATE_HOST_REQUIREMENTS true"
    "--set-default FONTCONFIG_FILE ${fonts}"
  ];

  preCheck = ''
    export PLAYWRIGHT_BROWSERS_PATH=${browsers} FONTCONFIG_FILE=${fonts} HOME=$TMPDIR
  '';

  passthru = { inherit browsers; };

  meta = {
    description = "HTTP API for Canvas LMS backed by a logged-in headless browser";
    mainProgram = "canvas-api";
    platforms = lib.platforms.linux;
  };
}
