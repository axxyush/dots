# TestRoomPlan

An iOS app that uses Apple's RoomPlan framework to scan a real room, visualize a generated floor plan, and export detailed structured scan data for accuracy analysis.

This project is useful for:
- Testing RoomPlan detection quality in different room types.
- Capturing wall/door/window/object geometry for analysis.
- Sharing a JSON report and rendered floor plan snapshot after each scan.

## What The App Does

1. Starts a RoomPlan capture session.
2. Processes the scan into a `CapturedRoom` model.
3. Displays results in two views:
- A 2D floor-plan style visualization.
- A detailed textual report with counts, dimensions, and object distances.
4. Exports scan output as:
- A timestamped JSON file.
- A rendered image of the floor-plan view.

## Features

- Native RoomPlan scanning flow with SwiftUI-based UI.
- Automatic scan-duration tracking.
- Floor plan renderer with:
- Walls (solid)
- Doors (blue)
- Windows (cyan dashed)
- Objects (orange bounding boxes + labels)
- Origin marker and 1-meter scale bar.
- Rich scan report including:
- Room size estimate (width x depth)
- Counts for walls, doors, windows, objects
- Object inventory entries
- Pairwise floor-plane object distance matrix
- Share sheet export for JSON + image.

## Tech Stack

- Language: Swift 5
- UI: SwiftUI (with UIKit bridge where needed)
- Core framework: RoomPlan
- Deployment target: iOS 17.0
- Project type: UIKit lifecycle + SwiftUI root view (`SceneDelegate` + `UIHostingController`)

## Requirements

### Hardware

- iPhone/iPad with LiDAR support (for RoomPlan).
- Typical supported examples: iPhone Pro models with LiDAR, modern iPad Pro models.

### Software

- macOS with a recent Xcode version that supports:
- iOS 17 SDK
- RoomPlan framework

### Permissions

- Camera permission is required and declared in `Info.plist`.

## Project Structure

```
TestRoomPlan/
  AppDelegate.swift            # App entry + scene config
  SceneDelegate.swift          # Hosts SwiftUI root view
  ContentView.swift            # Start screen + flow control
  ScanningView.swift           # Room capture UI bridge
  ResultsView.swift            # Floor plan/report tabs + sharing
  FloorPlanView.swift          # 2D canvas renderer
  RoomDataModels.swift         # Codable export schema
  RoomExporter.swift           # Conversion + metrics + JSON export
  Info.plist                   # Camera usage description, scene config
```

## Runtime Flow

1. App launches and shows `ContentView`.
2. If RoomPlan is supported, user can start scan.
3. `ScanningView` wraps `RoomCaptureView` using `UIViewRepresentable`.
4. On successful processing, app transitions to `ResultsView`.
5. `ResultsView` generates `ScanExportData` immediately via `RoomExporter.export(...)`.
6. User can inspect floor plan/report and share exports.

## Exported Data Model

Main payload: `ScanExportData`

Contains:
- `metadata`
- Timestamp
- Scan duration
- Total counts
- Computed room width/depth
- Bounding box
- `walls`
- Position, size, orientation (quaternion)
- `doors` / `windows`
- Position, size, orientation
- Nearest parent wall index
- `objects`
- Category, dimensions, confidence, position
- `distanceMatrix`
- Pairwise object distances in the floor plane (X/Z)
- `accuracyReport`
- Human-readable room size line
- Counts and formatted report lines

### JSON Output Location

Exported JSON is written to the app Documents directory with a filename format:

`roomplan_scan_yyyyMMdd_HHmmss.json`

## Visualization Details

The floor plan is drawn in `FloorPlanView` using `Canvas`:
- World-space X/Z is projected into screen-space.
- Global bounds are computed across walls, openings, objects, and origin.
- Uniform scaling keeps proportions accurate.
- A fixed 1m scale bar helps visual sanity checks.

## Run On iPhone (Step-by-Step From Clone)

Use these steps if you are starting from scratch on a Mac.

### 1) Clone the repository

1. Open Terminal.
2. Run:

```bash
git clone https://github.com/aryanmudgal-tech/TestRoomPlan.git
cd TestRoomPlan
```

### 2) Open the project in Xcode

1. Double-click `TestRoomPlan.xcodeproj`.
2. Wait for Xcode indexing to finish.

### 3) Connect your iPhone

1. Connect iPhone to Mac with USB (or wireless debugging if already configured).
2. Unlock the phone and tap **Trust This Computer** if prompted.

### 4) Confirm device compatibility

1. RoomPlan requires a LiDAR-capable device.
2. Use an iPhone Pro model with LiDAR (or compatible iPad Pro).
3. Do not use the simulator for real RoomPlan testing.

### 5) Configure signing (required)

1. In Xcode, select the blue project icon in navigator.
2. Select the `TestRoomPlan` target.
3. Open **Signing & Capabilities**.
4. Check **Automatically manage signing**.
5. Choose your Apple Developer Team from the **Team** dropdown.
6. If bundle ID conflicts, change it to a unique value (for example: `com.yourname.TestRoomPlan`).

### 6) Select your run destination

1. In the Xcode top bar, open the device/simulator selector.
2. Choose your connected iPhone.

### 7) Build and run

1. Press Run (play button) or `Cmd + R`.
2. If this is your first local build, Xcode may take extra time.

### 8) If iOS blocks launch (first-time developer app)

If you see an untrusted developer message on the phone:

1. On iPhone, go to **Settings > Privacy & Security**.
2. Enable **Developer Mode** (if shown), then restart when asked.
3. Go to **Settings > General > VPN & Device Management**.
4. Trust your developer certificate/profile.
5. Run again from Xcode.

### 9) Grant camera access

1. On first launch, allow Camera permission.
2. This is required for RoomPlan capture.

### 10) Perform a scan

1. Tap **Start Room Scan**.
2. Begin near the doorway if you want consistent origin alignment.
3. Move slowly and capture the full room perimeter.

### 11) View and export results

1. Open **Floor Plan** tab for 2D visualization.
2. Open **Report** tab for dimensions, counts, and object distances.
3. Tap Share icon to export:
- JSON file (`roomplan_scan_yyyyMMdd_HHmmss.json`)
- Floor plan image

## Accuracy Testing Tips

- Start near the doorway to keep origin placement consistent across tests.
- Move slowly and keep walls/edges in frame.
- Keep the device stable during turns.
- Scan under good lighting and avoid reflective clutter when possible.
- Repeat scans and compare exported JSON values for variance.

## Known Constraints

- Depends on LiDAR hardware and RoomPlan support.
- Accuracy can vary by lighting, room complexity, and scanning path.
- Parent wall mapping for doors/windows is nearest-center heuristic, not explicit topological matching.
- Distance matrix is planar (X/Z) and does not include Y-axis separation.

## Troubleshooting

- Start button unavailable:
- Device likely does not support RoomPlan/LiDAR.
- Scan cancels unexpectedly:
- Ensure camera permission is enabled and scanning area is sufficiently observable.
- Export item missing from share sheet:
- JSON save may fail if document write fails; retry scan/share.

## Privacy Notes

- The app processes room geometry locally using Apple frameworks.
- Exported artifacts are user-initiated via share sheet.
- No custom network upload logic exists in this project.

## Future Improvements

- Add explicit scan-state/error UI feedback.
- Add regression mode for comparing two scans.
- Persist scan history in-app.
- Add CSV export for quick spreadsheet analysis.
- Improve wall-opening association beyond nearest-wall heuristic.

## License

No license file is currently included in this repository. Add a `LICENSE` file if you want to define usage terms.