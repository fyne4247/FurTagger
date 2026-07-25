# Homebrew cask skeleton for FurTag (personal tap first).
#
# This distributes a finished, signed, notarized .app — it is not part of the
# GUI runtime. Only publish after:
#   1. Versioned immutable release archive + SHA-256
#   2. Stable download URL
#   3. macOS signing + notarization verified
#   4. Credential persistence validated on the signed build
#
# Install from a personal tap:
#   brew tap <you>/furtag
#   brew install --cask furtag
#
# cask "furtag" do
#   version "1.0.0"
#   sha256 "REPLACE_WITH_SHA256_OF_RELEASE_ARCHIVE"
#
#   url "https://github.com/<you>/FurTag/releases/download/v#{version}/FurTag-#{version}-macOS.zip"
#   name "FurTag"
#   desc "Reverse-image tagger for furry/booru sources into Hydrus Network"
#   homepage "https://github.com/<you>/FurTag"
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
# NOTE: This file is intentionally commented out so `brew` does not install a
# placeholder. Uncomment and fill version/sha256/url when the first signed
# release is published.

cask "furtag" do
  version "0.0.0-dev"
  sha256 :no_check

  # Placeholder — replace before publishing.
  url "https://example.com/FurTag-#{version}-macOS.zip"
  name "FurTag"
  desc "Reverse-image tagger for furry/booru sources into Hydrus Network"
  homepage "https://github.com/example/FurTag"

  depends_on macos: ">= :big_sur"

  app "FurTag.app"

  caveats <<~EOS
    This cask is a development scaffold. Do not publish until a signed,
    notarized, versioned release archive with a real SHA-256 is available.
  EOS
end
