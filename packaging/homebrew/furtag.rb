# Homebrew cask skeleton for FurTag (personal tap first).
#
# This distributes a finished, signed, notarized .app — it is not part of the
# GUI runtime. Only publish after:
#   1. Versioned immutable release archive + SHA-256
#   2. Stable download URL
#   3. macOS signing + notarization verified
#   4. Credential persistence validated on the signed build
#
# Install from a personal tap (after a real signed release exists):
#   brew tap fyne4247/furtag
#   brew install --cask furtag
#
# Real cask body (fill after notarized zip is on a GitHub Release):
#
# cask "furtag" do
#   version "0.1.0"
#   sha256 "REPLACE_WITH_SHA256_OF_RELEASE_ARCHIVE"
#
#   url "https://github.com/fyne4247/FurTagger/releases/download/v#{version}/FurTag-#{version}-macOS.zip"
#   name "FurTag"
#   desc "Reverse-image tagger for furry/booru sources into Hydrus Network"
#   homepage "https://github.com/fyne4247/FurTagger"
#
#   depends_on macos: ">= :big_sur"
#
#   app "FurTag.app"
#
#   zap trash: [
#     "~/Library/Application Support/FurTag",
#     "~/Library/Preferences/org.furtag.FurTag.plist",
#   ]
# end
#
# Work estimate: the Ruby cask itself is ~30 minutes once the zip exists.
# The hard part is packaging + Developer ID signing + notarization + a
# credential-persistence smoke test on the signed app (hours first time;
# packaging/README.md). Without that, brew users get a broken unsigned app.

cask "furtag" do
  version "0.0.0-dev"
  sha256 :no_check

  # Placeholder — replace before publishing.
  url "https://github.com/fyne4247/FurTagger/releases/download/v#{version}/FurTag-#{version}-macOS.zip"
  name "FurTag"
  desc "Reverse-image tagger for furry/booru sources into Hydrus Network"
  homepage "https://github.com/fyne4247/FurTagger"

  depends_on macos: ">= :big_sur"

  app "FurTag.app"

  caveats <<~EOS
    This cask is a development scaffold. Do not publish until a signed,
    notarized, versioned release archive with a real SHA-256 is available.
    Until then: clone the repo and run ./FurTag-GUI.command
  EOS
end
