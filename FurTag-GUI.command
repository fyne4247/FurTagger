#!/bin/bash
# Double-clickable GUI launcher (always opens the desktop app).
cd "$(dirname "$0")" || exit 1
exec ./FurTag.command gui
