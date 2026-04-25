import SwiftUI

struct SetupInstructionsView: View {
    let onBegin: () -> Void
    let onCancel: () -> Void

    var body: some View {
        ZStack {
            DotsTheme.background.ignoresSafeArea()

            VStack(alignment: .leading, spacing: 22) {
                DotsWordmark(textSize: 28, dotDiameter: 8, weight: .semibold)

                VStack(alignment: .leading, spacing: 10) {
                    Text("Start at the entry.")
                        .font(.system(size: 34, weight: .bold, design: .rounded))
                        .foregroundStyle(DotsTheme.primaryText)

                    Text("Begin the scan while standing in the doorway that will be used as the saved entry point. Keep that doorway in view during the first sweep so the room origin lines up with the true entry.")
                        .font(.body)
                        .foregroundStyle(DotsTheme.secondaryText)
                }

                VStack(alignment: .leading, spacing: 12) {
                    instructionRow(icon: "door.left.hand.open", text: "Stand just inside the entry and face into the room.")
                    instructionRow(icon: "camera.viewfinder", text: "Keep the doorway visible for the first few seconds of the scan.")
                    instructionRow(icon: "move.3d", text: "Move slowly around the perimeter to help RoomPlan keep object distances stable.")
                    instructionRow(icon: "checkmark.seal", text: "After processing, confirm which detected door is the real entry before saving the room model locally.")
                }
                .padding(18)
                .background(
                    RoundedRectangle(cornerRadius: 18)
                        .fill(DotsTheme.panel)
                )
                .overlay(
                    RoundedRectangle(cornerRadius: 18)
                        .stroke(DotsTheme.border, lineWidth: 1)
                )

                Spacer()

                Button(action: onBegin) {
                    HStack {
                        Image(systemName: "viewfinder")
                        Text("Begin Entry Scan")
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(DotsPrimaryButtonStyle())
                .accessibilityLabel("Begin entry scan")
                .accessibilityHint("Starts RoomPlan scanning after reviewing the doorway instructions.")

                Button(action: onCancel) {
                    Text("Back")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(DotsSecondaryButtonStyle())
            }
            .padding(24)
        }
        .preferredColorScheme(.dark)
    }

    private func instructionRow(icon: String, text: String) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: icon)
                .font(.system(size: 18, weight: .semibold))
                .foregroundStyle(.white)
                .frame(width: 24, height: 24)

            Text(text)
                .font(.body)
                .foregroundStyle(DotsTheme.primaryText)
        }
    }
}
