#!/bin/bash
# Consolidates changelog entries into CHANGELOG.md
# Run from project root: ./changelog/create_changelog.sh

set -e

CHANGELOG_DIR="changelog"
OUTPUT_FILE="CHANGELOG.md"

cat > "$OUTPUT_FILE" << 'EOF'
# Changelog

All notable changes to this project are documented here.

EOF

for file in $(ls -r "$CHANGELOG_DIR"/*.changelog.md 2>/dev/null); do
    if [ -f "$file" ]; then
        date=$(basename "$file" .changelog.md)
        echo "## $date" >> "$OUTPUT_FILE"
        echo "" >> "$OUTPUT_FILE"
        cat "$file" >> "$OUTPUT_FILE"
        echo "" >> "$OUTPUT_FILE"
    fi
done

echo "CHANGELOG.md updated from $CHANGELOG_DIR entries"
