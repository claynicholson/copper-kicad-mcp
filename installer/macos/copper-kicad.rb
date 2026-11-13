# Homebrew formula for Copper for KiCad (tap: claynicholson/homebrew-copper, file Formula/copper-kicad.rb).
#
#   brew tap claynicholson/copper
#   brew install copper-kicad
#   copper setup
#
# STATUS: NOT PUBLISHED, UNTESTED (no tap exists; do not document `brew install` until one does). Written on Windows from Homebrew's documented formula API; it has not been run through
# `brew install`, `brew audit` or `brew test` on a Mac. Before publishing the tap:
#   1. replace `url`/`sha256` with the wheel of the release being published (the hash is the wheel's line in
#      that release's SHA256SUMS.txt; the value below is a placeholder and will fail the download check);
#   2. set the `python@…` dependency to a Python that `brew info pillow` lists as supported that day;
#   3. run `brew install --build-from-source ./copper-kicad.rb && brew test copper-kicad && brew audit --new copper-kicad`.
#
# Design: Copper is pure Python and its only runtime dependency is Pillow, which Homebrew already ships as a
# formula. The virtualenv therefore sees Homebrew's site-packages (system_site_packages) and only Copper's
# own wheel is installed into it; nothing is downloaded from PyPI at install time.
class CopperKicad < Formula
  include Language::Python::Virtualenv

  desc "MCP server that lets AI agents design real, orderable KiCad boards"
  homepage "https://coppereda.com"
  url "https://github.com/claynicholson/copper-kicad-mcp/releases/download/v1.1.1/copper_kicad-1.1.1-py3-none-any.whl",
      using: :nounzip
  sha256 "0000000000000000000000000000000000000000000000000000000000000000" # placeholder: stamp at release
  license "MIT"

  depends_on "pillow"
  depends_on "python@3.13"

  def install
    venv = virtualenv_create(libexec, "python3.13", system_site_packages: true)
    wheel = Dir["*.whl"].first
    odie "the Copper wheel was not downloaded" if wheel.nil?
    system libexec/"bin/python", "-m", "pip", "install", "--no-deps", "--no-index", wheel
    bin.install_symlink libexec/"bin/copper"
    bin.install_symlink libexec/"bin/copper-kicad"
  end

  def caveats
    <<~EOS
      Copper needs KiCad 9 or 10 (https://www.kicad.org/download/macos/); it is not installed by this formula.

      Finish in your browser (KiCad found, what to bring along from it, your AI apps, a test call):
        copper setup

      Nothing in KiCad's own settings is changed without asking, and `copper setup --undo` puts it back.
      Update with `brew upgrade copper-kicad`.
    EOS
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/copper version")
    # the MCP server answers a ping on stdio
    out = pipe_output("#{bin}/copper serve", "{\"jsonrpc\":\"2.0\",\"id\":7,\"method\":\"ping\"}\n", 0)
    assert_match "\"id\": 7", out
  end
end
