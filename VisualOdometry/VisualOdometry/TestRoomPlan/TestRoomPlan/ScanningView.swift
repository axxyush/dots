import SwiftUI
import RoomPlan

struct ScanningView: View {
    let onComplete: (CapturedRoom, TimeInterval) -> Void
    let onCancel: () -> Void
    @ObservedObject var scanManager: ScanManager

    @State private var scanStartTime = Date()
    @State private var stopRequested = false
    @State private var showMenu = false
    @State private var flashOverlay = false

    var body: some View {
        ZStack {
            if RoomCaptureSession.isSupported {
                RoomCaptureViewRepresentable(
                    onComplete: { room in
                        let duration = Date().timeIntervalSince(scanStartTime)
                        onComplete(room, duration)
                    },
                    onCancel: onCancel,
                    stopRequested: $stopRequested,
                    scanManager: scanManager
                )
                .ignoresSafeArea()
            } else {
                Color.black
                    .ignoresSafeArea()

                VStack(spacing: 14) {
                    Label("RoomPlan Is Not Supported", systemImage: "exclamationmark.triangle.fill")
                        .font(.headline)
                        .foregroundStyle(.white)

                    Text("Use a LiDAR-capable iPhone or iPad to access scanning.")
                        .font(.subheadline)
                        .foregroundStyle(.white.opacity(0.8))
                        .multilineTextAlignment(.center)

                    Button("Back") {
                        onCancel()
                    }
                    .buttonStyle(.borderedProminent)
                }
                .padding(24)
            }

            // Flash effect when photo is taken
            if flashOverlay {
                Color.white
                    .ignoresSafeArea()
                    .allowsHitTesting(false)
                    .transition(.opacity)
            }

            // Top-left menu button
            VStack {
                HStack {
                    if !stopRequested {
                        Menu {
                            Button {
                                withAnimation(.easeInOut(duration: 0.15)) { flashOverlay = true }
                                scanManager.takePhoto()
                                DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) {
                                    withAnimation(.easeInOut(duration: 0.15)) { flashOverlay = false }
                                }
                            } label: {
                                Label("Take Photo (\(scanManager.capturedPhotos.count))", systemImage: "camera")
                            }

                            Button(role: .destructive) {
                                onCancel()
                            } label: {
                                Label("Cancel Scan", systemImage: "xmark.circle")
                            }

                            Button {
                                stopRequested = true
                            } label: {
                                Label("Done Scanning", systemImage: "checkmark.circle")
                            }
                        } label: {
                            Image(systemName: "ellipsis.circle.fill")
                                .font(.system(size: 32))
                                .symbolRenderingMode(.palette)
                                .foregroundStyle(.white, .black.opacity(0.5))
                                .shadow(radius: 4)
                        }
                        .padding(.leading, 16)
                        .padding(.top, 8)
                    }

                    Spacer()

                    // Photo count badge
                    if !stopRequested && scanManager.capturedPhotos.count > 0 {
                        HStack(spacing: 4) {
                            Image(systemName: "camera.fill")
                                .font(.caption2)
                            Text("\(scanManager.capturedPhotos.count)")
                                .font(.caption.bold())
                        }
                        .padding(.horizontal, 10)
                        .padding(.vertical, 5)
                        .background(.ultraThinMaterial)
                        .cornerRadius(12)
                        .padding(.trailing, 16)
                        .padding(.top, 8)
                    }
                }
                Spacer()

                // Scan odometry stats overlay
                if !stopRequested {
                    HStack(spacing: 14) {
                        scanStatChip(
                            icon: "figure.walk",
                            value: String(format: "%.1f m", scanManager.trackerState.distanceWalked)
                        )
                        scanStatChip(
                            icon: "shoeprints.fill",
                            value: "\(scanManager.trackerState.stepsTaken) steps"
                        )
                        scanStatChip(
                            icon: "location.north.fill",
                            value: "\(Int(scanManager.trackerState.heading))° \(CompassUtilities.directionString(for: scanManager.trackerState.heading))"
                        )
                    }
                    .padding(.horizontal, 16)
                    .padding(.bottom, 12)
                }
            }

            // Processing indicator
            if stopRequested {
                VStack {
                    Spacer()
                    Label("Processing scan…", systemImage: "waveform")
                        .font(.subheadline)
                        .foregroundStyle(.white)
                        .padding(.bottom, 52)
                }
            }
        }
    }

    private func scanStatChip(icon: String, value: String) -> some View {
        HStack(spacing: 5) {
            Image(systemName: icon)
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.white.opacity(0.7))
            Text(value)
                .font(.caption.weight(.semibold).monospacedDigit())
                .foregroundStyle(.white)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(.black.opacity(0.6))
        .clipShape(Capsule())
    }
}
