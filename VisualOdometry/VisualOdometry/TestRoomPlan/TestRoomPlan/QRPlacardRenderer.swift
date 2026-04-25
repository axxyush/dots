import Foundation
import UIKit
import CoreImage
import CoreImage.CIFilterBuiltins
import ARKit

struct QRPlacardAsset: Equatable {
    let image: UIImage
    let pngData: Data
}

enum QRPlacardRenderer {
    static let templateVersion = RoomModelExporter.markerTemplateVersion
    static let physicalWidthMeters = CGFloat(RoomModelExporter.markerPhysicalWidthMeters)

    static func renderPlacard(uuid: String, size: CGSize = CGSize(width: 1200, height: 1200)) -> QRPlacardAsset? {
        let format = UIGraphicsImageRendererFormat.default()
        format.opaque = true
        format.scale = 1

        let renderer = UIGraphicsImageRenderer(size: size, format: format)
        let pngData = renderer.pngData { context in
            let cg = context.cgContext
            let rect = CGRect(origin: .zero, size: size)

            UIColor.white.setFill()
            cg.fill(rect)

            UIColor.black.setStroke()
            cg.setLineWidth(size.width * 0.02)
            cg.stroke(rect.insetBy(dx: size.width * 0.03, dy: size.height * 0.03))

            let title = "DOTS ENTRY MARKER"
            let subtitle = uuid.uppercased()
            let titleAttributes: [NSAttributedString.Key: Any] = [
                .font: UIFont.monospacedSystemFont(ofSize: size.width * 0.05, weight: .bold),
                .foregroundColor: UIColor.black
            ]
            let subtitleAttributes: [NSAttributedString.Key: Any] = [
                .font: UIFont.monospacedSystemFont(ofSize: size.width * 0.032, weight: .regular),
                .foregroundColor: UIColor.black
            ]

            let titleSize = title.size(withAttributes: titleAttributes)
            let subtitleSize = subtitle.size(withAttributes: subtitleAttributes)
            let titleOrigin = CGPoint(x: (size.width - titleSize.width) / 2, y: size.height * 0.08)
            let subtitleOrigin = CGPoint(x: (size.width - subtitleSize.width) / 2, y: size.height * 0.84)
            title.draw(at: titleOrigin, withAttributes: titleAttributes)
            subtitle.draw(at: subtitleOrigin, withAttributes: subtitleAttributes)

            if let qrImage = qrCodeImage(for: uuid, targetSide: size.width * 0.58) {
                let qrOrigin = CGPoint(x: (size.width - qrImage.size.width) / 2, y: size.height * 0.2)
                qrImage.draw(at: qrOrigin)
            }

            let footer = "Print at 10 cm width and place at the room entry."
            let footerAttributes: [NSAttributedString.Key: Any] = [
                .font: UIFont.systemFont(ofSize: size.width * 0.024, weight: .medium),
                .foregroundColor: UIColor.darkGray
            ]
            let footerSize = footer.size(withAttributes: footerAttributes)
            let footerOrigin = CGPoint(x: (size.width - footerSize.width) / 2, y: size.height * 0.92)
            footer.draw(at: footerOrigin, withAttributes: footerAttributes)
        }

        guard let image = UIImage(data: pngData) else { return nil }
        return QRPlacardAsset(image: image, pngData: pngData)
    }

    static func referenceImage(for uuid: String) -> ARReferenceImage? {
        guard
            let asset = renderPlacard(uuid: uuid),
            let cgImage = asset.image.cgImage
        else {
            return nil
        }

        let referenceImage = ARReferenceImage(
            cgImage,
            orientation: CGImagePropertyOrientation.up,
            physicalWidth: physicalWidthMeters
        )
        referenceImage.name = uuid
        return referenceImage
    }

    private static func qrCodeImage(for uuid: String, targetSide: CGFloat) -> UIImage? {
        let filter = CIFilter.qrCodeGenerator()
        filter.message = Data(uuid.utf8)
        filter.correctionLevel = "H"

        let context = CIContext(options: nil)
        guard let outputImage = filter.outputImage else { return nil }
        let integralExtent = outputImage.extent.integral
        let scale = min(targetSide / integralExtent.width, targetSide / integralExtent.height)
        let transformed = outputImage.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
        guard let cgImage = context.createCGImage(transformed, from: transformed.extent) else { return nil }
        return UIImage(cgImage: cgImage)
    }
}
