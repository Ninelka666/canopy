# ORE DELVE

A single-file mining game: a miner with a pickaxe works a procedural dungeon,
hauls ore up the lift, sells it, buys food, water and better gear, and goes deeper.
No build step, no dependencies, no CDN — open `index.html` in a browser.

## The loop

1. **Dig.** Walk into rock to swing the pickaxe. Dirt and stone are quick; ore takes longer
   and needs a pickaxe strong enough for it (a Rusty Pickaxe will never crack Gold).
2. **Survive.** Food and water drain the whole time you are underground and drain faster
   while you swing. At zero they eat your health. Gas pockets, rockfalls, lava and cave
   creatures do the rest.
3. **Haul.** The backpack has a hard capacity. Ride the amber lift tile back to the surface camp.
4. **Trade.** Sell ore at the **Ore Trader**, eat and drink at the **Diner & Bunk**,
   upgrade at the **Gear Shop** (pickaxe, head lamp, backpack, boots, helmet, canteen, rations).
5. **Descend.** Find the cyan stairs down on each level to open the next depth. The **Mine Lift**
   in town then drops you straight to any depth you have already opened.

Twelve materials, from Rock and Coal up to Diamond and Mythril; each one appears at its own
depth, needs its own pickaxe tier and sells for its own price.

## Continuing a run

Progress (money, gear, best depth, backpack, vitals, statistics) is saved to the browser's
local storage automatically — on every trade, on every lift ride, on pause, and every 25 seconds.
The start panel shows the save and lets you continue it or start a new mine.
Levels are regenerated from the world seed, so the same seed always digs the same mine.

## Controls

| Key | Action |
| --- | --- |
| `WASD` / arrows | Walk — push into rock to mine it, into a creature to attack it |
| `E` | Use the lift, the stairs down, or a building door |
| `ENTER` | Confirm in a panel (mouse clicks work too) |
| `M` | Level map |
| `ESC` | Pause, save, abandon the run, or start over |

## Start panel

Before the game starts you pick the render resolution (720p → 4K), the pixel density
(1× / 2× / 3× / device), the world seed, and one of three output modes:

- **PLAY** — plain realtime run, for capturing the screen with an external recorder.
- **RECORD** — records the canvas itself with `MediaRecorder`, H.264 preferred
  (falls back to VP9/WebM), ~48 Mbit/s, live fps and elapsed time in the badge.
  `STOP` in the badge ends the take and downloads the file.
- **EXPORT PNG FRAMES** — deterministic dump: the simulation is advanced by exactly 1/60 s
  per frame and each frame is written as `frame_000000.png`, `frame_000001.png`, … into a
  folder you pick through the File System Access API (Chrome/Edge, https or localhost).
  Stitch them with e.g. `ffmpeg -framerate 60 -i frame_%06d.png -c:v libx264 -crf 16 out.mp4`.

Everything — the game, the UI, the sound — lives in `index.html`.
