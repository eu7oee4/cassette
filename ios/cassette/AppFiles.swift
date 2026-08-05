import UIKit

/// 沙盒文件路径工具。
///
/// 关键：iOS app 的容器路径（含一个 UUID）在**重装/更新时可能改变**，
/// 所以【绝对路径不能持久化】。消息里存的老绝对 URL 重装后会失效。
/// 这里每次用图时，按「`/Documents/` 之后的相对部分」重新拼到**当前** Documents，
/// 从而既修好历史里的老路径，也让以后不再丢图。
enum AppFiles {
    static var documents: URL {
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
    }

    /// 把（可能来自旧容器的）URL 重新锚定到当前 Documents。
    static func reanchored(_ url: URL) -> URL {
        let path = url.path
        if let r = path.range(of: "/Documents/") {
            let relative = String(path[r.upperBound...])
            return documents.appendingPathComponent(relative)
        }
        return url
    }

    /// 从消息的存储 URL 加载图片（自动重新锚定当前容器）。
    static func loadImage(_ url: URL) -> UIImage? {
        UIImage(contentsOfFile: reanchored(url).path)
    }
}
