#!/usr/bin/env bash
# grim wrapper — adds web image formats (jpeg, webp, avif, gif, tiff, bmp, heic)
# on top of the system grim, which on this host is built with png/ppm only.
#
# Native formats (png, ppm), --help, and any option this wrapper doesn't
# recognise are passed straight through to the real grim, so existing behaviour
# is byte-for-byte unchanged. Web formats are produced by capturing a lossless
# PPM from the real grim and encoding it with ImageMagick.
#
# The requested format comes from `-t <fmt>`, or is inferred from the output
# file's extension (e.g. `grim shot.webp`). Delete this file to revert to the
# stock grim.

set -euo pipefail

REAL_GRIM=/usr/bin/grim

passthrough() { exec "$REAL_GRIM" "$@"; }

[ -x "$REAL_GRIM" ] || { echo "grim: real binary not found at $REAL_GRIM" >&2; exit 1; }

# ---- parse grim's CLI (getopt-compatible: option bundling, attached values,
#      and options appearing after the output file are all handled) ----
orig=("$@")
scale=() geometry=() outname=() cursor=()
type="" quality="" level=()
outfile="" have_outfile=0

i=0; n=${#orig[@]}
while [ "$i" -lt "$n" ]; do
	a=${orig[$i]}
	if [ "$a" = "--" ]; then
		i=$((i + 1))
		while [ "$i" -lt "$n" ]; do outfile=${orig[$i]}; have_outfile=1; i=$((i + 1)); done
		break
	elif [ -z "$a" ] || [ "$a" = "-" ] || [ "${a:0:1}" != "-" ]; then
		outfile=$a; have_outfile=1; i=$((i + 1))
	else
		rest=${a#-}
		while [ -n "$rest" ]; do
			opt=${rest:0:1}; rest=${rest:1}
			case $opt in
			h)
				# Show the real help plus the wrapper's additions, then exit.
				"$REAL_GRIM" -h || true
				cat >&2 <<-'EOF'

					Wrapper additions (encoded with ImageMagick):
					  -t jpeg|webp|avif|gif|tiff|bmp|heic   extra output formats
					  The format is also inferred from the output file's extension,
					  e.g. `grim shot.webp` or `grim -q 90 photo.avif`.
				EOF
				exit 0
				;;
			c) cursor=(-c) ;;
			s | g | t | q | l | o)
				val=$rest; rest=""
				if [ -z "$val" ]; then
					i=$((i + 1))
					[ "$i" -lt "$n" ] || { echo "grim: option -$opt requires an argument" >&2; exit 1; }
					val=${orig[$i]}
				fi
				case $opt in
				s) scale=(-s "$val") ;;
				g) geometry=(-g "$val") ;;
				t) type=$val ;;
				q) quality=$val ;;
				l) level=(-l "$val") ;;
				o) outname=(-o "$val") ;;
				esac
				;;
			*) passthrough "${orig[@]}" ;; # unknown option: defer entirely to real grim
			esac
		done
		i=$((i + 1))
	fi
done

# ---- resolve the requested format ----
fmt=$(printf '%s' "$type" | tr '[:upper:]' '[:lower:]')
[ "$fmt" = "jpg" ] && fmt="jpeg"

if [ -z "$fmt" ] && [ "$have_outfile" -eq 1 ] && [ "$outfile" != "-" ]; then
	ext=$(printf '%s' "${outfile##*.}" | tr '[:upper:]' '[:lower:]')
	case "$ext" in
	jpg | jpeg) fmt=jpeg ;;
	webp) fmt=webp ;;
	avif) fmt=avif ;;
	gif) fmt=gif ;;
	tif | tiff) fmt=tiff ;;
	bmp) fmt=bmp ;;
	heic | heif) fmt=heic ;;
	esac
fi

# Native or unspecified formats: hand the original argv to the real grim
# untouched (preserves default filenames, stdout, -l, geometry-from-stdin, ...).
case "$fmt" in
"" | png | ppm) passthrough "${orig[@]}" ;;
esac

# ---- from here on it's a web format that needs ImageMagick ----
if command -v magick >/dev/null 2>&1; then
	IM=(magick)
elif command -v convert >/dev/null 2>&1; then
	IM=(convert)
else
	echo "grim: '$fmt' output needs ImageMagick, which isn't installed." >&2
	echo "      sudo apt-get install -y imagemagick libmagickcore-7.q16-10-extra" >&2
	exit 1
fi

qopt=()
case "$fmt" in
jpeg | webp | avif | heic) qopt=(-quality "${quality:-80}") ;;
esac

# ---- resolve the output target (force the coder so the bytes match the name) ----
if [ "$have_outfile" -eq 1 ] && [ "$outfile" = "-" ]; then
	target="${fmt}:-"
elif [ "$have_outfile" -eq 1 ]; then
	target="${fmt}:${outfile}"
else
	# Replicate grim's default: timestamped name in GRIM_DEFAULT_DIR /
	# XDG_PICTURES_DIR / cwd, with the requested extension.
	dir="${GRIM_DEFAULT_DIR:-${XDG_PICTURES_DIR:-$PWD}}"
	outfile="${dir%/}/$(date +%Y%m%d_%Hh%Mm%Ss)_grim.${fmt}"
	target="${fmt}:${outfile}"
fi

# ---- capture a lossless PPM from the real grim, encode with ImageMagick ----
set -o pipefail
"$REAL_GRIM" "${scale[@]}" "${geometry[@]}" "${outname[@]}" "${cursor[@]}" -t ppm - |
	"${IM[@]}" ppm:- "${qopt[@]}" "$target"
