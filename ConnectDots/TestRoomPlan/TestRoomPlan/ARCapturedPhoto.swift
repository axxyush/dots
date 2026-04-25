import UIKit
import simd

struct ARCapturedPhoto {
    let image: UIImage
    let transform: simd_float4x4
    let intrinsics: simd_float3x3
    let timestamp: TimeInterval
}
