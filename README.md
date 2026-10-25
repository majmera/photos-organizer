# photos-organizer

Cleans up a Google Photos library from a Google Takeout export, entirely on your own computer.

- **Duplicates:** finds copies of the same picture, such as WhatsApp or email copies, resized shares, and re-saved files. It keeps the best copy.
- **Similar shots:** groups bursts and retakes of the same scene. Each shot is scored on focus, exposure, closed eyes and faces cut off at the edge, and the tool proposes keeping the best one or two.
- **Albums:** suggests event and trip albums from photo dates and locations. Scenes already covered by one of your albums are skipped.
- **Review:** you check every proposal on a local web page before anything happens.
- **Cleanup lists:** it writes a list of photos to trash and albums to create, with links that find each photo in Google Photos.

Google's APIs don't let third-party apps delete photos or add existing photos to albums (since March 2025). So for now you apply the changes yourself in Google Photos, using the lists. Automating that step through the browser is planned for later.

## Setup (macOS)

You need Python 3.10 or newer (`brew install python` if you don't have it).

```sh
git clone https://github.com/majmera/photos-organizer
cd photos-organizer
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
# Optional: name albums after places ("Lisbon, 20–22 Mar 2024") instead of "Trip, 20–22 Mar 2024"
pip install -e ".[places]"
```

## 1. Export your library

1. Go to [takeout.google.com](https://takeout.google.com), click **Deselect all**, and tick only **Google Photos**.
2. Choose **.zip** with the largest file size, and create the export. Google emails you when it is ready, which can take hours.
3. Download every part and unzip them all into **one** folder, for example `~/Takeout-photos`. On a Mac, double-clicking each zip works. If you're asked, merge the folders.

## 2. Analyze

```sh
photos-organizer run ~/Takeout-photos
```

This reads the export, analyzes every photo, and builds the duplicate groups and album suggestions. It stores everything in `photos-organizer.db` in the current folder. Your export is only read, never changed. Re-running is safe: photos that were already analyzed are skipped.

Expect a few minutes per thousand photos on a laptop. Videos are indexed and included in albums, but not checked for duplicates yet.

## 3. Review

```sh
photos-organizer review
```

This opens the review page in your browser at `http://127.0.0.1:8765`, and only your computer can reach it. There are three tabs:

- **Similar shots:** green photos are kept and red ones go on the trash list. Click the button under a photo to change it, and click a photo to see it large.
- **Duplicates:** copies of the same picture. The copy with the most pixels is kept.
- **Suggested albums:** untick an album to leave it out, or edit its name.

Your choices are saved as you click. They also survive a re-run with different settings.

## 4. Apply in Google Photos

```sh
photos-organizer export
```

This writes a `cleanup/` folder with two lists:

- **`trash.html`:** photos to delete, grouped by day. Each filename links to a Google Photos search for that file. Delete the photo there, then tick it off. Ticks are remembered, so you can work through the list over several sittings. Google keeps deleted photos in the trash for 60 days.
- **`albums.html`:** albums to create, each with a name and a date range. In Google Photos, create the album, choose **Add photos**, and select everything from the first date to the last.

There are also `trash.csv` and `albums.csv` with the same content.

## Tuning

| Option | Default | Effect |
| --- | --- | --- |
| `--window` | 60 | Seconds between shots that can count as one scene |
| `--similarity` | 20 | How alike shots must look (lower is stricter, out of 64) |
| `--keep` | 2 | Most shots kept per similar group. A second shot is kept only if it scores within 10% of the best |
| `--gap-hours` | 6 | A pause this long starts a new event |
| `--min-items` | 10 | Smallest event worth an album |

To re-group without re-analyzing, run for example `photos-organizer group --keep 1`.

## How it works

| Step | Module | What it does |
| --- | --- | --- |
| Import | `takeout.py` | Pairs each file with its JSON sidecar, and handles truncated names and `(1)` clashes. It merges the copies Takeout writes into every album, and attaches `-edited` versions to their originals. |
| Analyze | `analyze.py` | Computes a pixel hash plus perceptual hashes (pHash, dHash). It measures focus (Laplacian variance on faces or the sharpest tiles), exposure, faces, open eyes (OpenCV Haar cascades), and faces cut off at the frame edge. |
| Group | `grouping.py` | Duplicates are pixel-identical or near-identical hashes. Similar shots are photos taken within 60 s whose hashes are close, ranked by a weighted score. |
| Albums | `events.py` | Splits the timeline at long pauses and finds your home location. Events away from home on consecutive days are merged into trips. |
| Review | `review/` | A standard-library web server and a single HTML page. |

## Known limits

- Face and eye detection uses OpenCV's classic detectors. They are fast and need no download, but they miss small, tilted or profile faces. Focus and exposure carry most of the score.
- Photos with no date (rare in Takeout) are left out of similar-shot groups and albums.
- RAW files are indexed but can't be analyzed yet.

## Development

```sh
pip install -e ".[dev]"
pytest
```
