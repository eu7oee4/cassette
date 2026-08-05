import SwiftUI

/// 一张表情包。图片文件按 id 存在沙盒里。num 是永久序号（稳定，删了留空、新的取 max+1），
/// 给后端当代号 s{num}——这样删表情不会让模型认错张。
struct Sticker: Identifiable, Codable, Equatable {
    let id: String
    var description: String
    var num: Int

    /// 给后端的代号（computed，不入 Codable；后端按 num 算 s{num}）。
    var handle: String { "s\(num)" }
}

/// 表情包库：图片存沙盒 Documents/Stickers/，清单存 manifest.json。
@MainActor
final class StickerStore: ObservableObject {
    @Published private(set) var stickers: [Sticker] = []
    @Published private(set) var describingIds: Set<String> = []   // 正在生成描述的（UI 显示"生成中"）

    private let fileManager = FileManager.default
    private let service = ChatService()

    /// 按 id 找表情。
    func sticker(id: String) -> Sticker? { stickers.first { $0.id == id } }

    /// 表情包目录：Documents/Stickers/
    private var directory: URL {
        AppFiles.documents.appendingPathComponent("Stickers", isDirectory: true)
    }

    /// 清单文件：Documents/Stickers/manifest.json
    private var manifestURL: URL {
        directory.appendingPathComponent("manifest.json")
    }

    init() {
        ensureDirectory()
        load()
        seedDefaultsIfNeeded()
    }

    /// 某张表情包对应的本地图片文件 URL。
    func imageURL(for sticker: Sticker) -> URL {
        directory.appendingPathComponent("\(sticker.id).png")
    }

    /// 从相册选中的图片数据新增一张表情包：写图片文件 + 更新清单。
    /// 返回新建的 Sticker（失败返回 nil）。
    @discardableResult
    func addSticker(from imageData: Data) -> Sticker? {
        // 统一转成 PNG 存储，兼容各种来源格式。先缩到 640——表情最大显示 160pt，
        // 原图 12MP 转 PNG 会是几十 MB 落盘 + 全尺寸 base64 发描述接口。
        guard let uiImage = UIImage(data: imageData)?.downscaled(maxDimension: 640),
              let png = uiImage.pngData() else {
            return nil
        }
        let num = (stickers.map(\.num).max() ?? 0) + 1   // 永久序号：取当前最大 +1，不复用删掉的
        let sticker = Sticker(id: UUID().uuidString, description: "", num: num)
        let url = imageURL(for: sticker)
        do {
            try png.write(to: url, options: .atomic)
        } catch {
            print("写入表情包图片失败: \(error)")
            return nil
        }
        stickers.append(sticker)
        saveManifest()
        generateDescription(for: sticker.id, imageData: png)   // 让模型看图写一句描述
        return sticker
    }

    /// 改某张的描述（用户手动改 / 模型改，都走这里）。
    func updateDescription(id: String, _ description: String) {
        guard let i = stickers.firstIndex(where: { $0.id == id }) else { return }
        stickers[i].description = description
        saveManifest()
    }

    /// 删一张：删图片文件 + 更新清单。
    func remove(id: String) {
        guard let i = stickers.firstIndex(where: { $0.id == id }) else { return }
        try? fileManager.removeItem(at: imageURL(for: stickers[i]))
        stickers.remove(at: i)
        saveManifest()
    }

    /// 调后端让模型看图写一句描述（异步，完成后回填）。
    private func generateDescription(for id: String, imageData: Data) {
        describingIds.insert(id)
        Task {
            let desc = (try? await service.describeSticker(imageData: imageData)) ?? ""
            if !desc.isEmpty { updateDescription(id: id, desc) }
            describingIds.remove(id)
        }
    }

    // MARK: - 内置默认表情包

    /// 首次启动（还没有清单文件）时，把 app 内置的默认表情包种进库。
    /// 内置包放在 bundle 的 DefaultStickers/ 里，**文件名（去扩展名）就是初始描述**——
    /// 不用现调后端，之后用户可改可删，跟自己加的表情完全一样。
    private func seedDefaultsIfNeeded() {
        guard !fileManager.fileExists(atPath: manifestURL.path) else { return }
        // 同步分组会把资源拍平进 bundle 根（子目录不保留），所以先按子目录找、
        // 空了再找根——注意 urls(...) 找不到返回的是**空数组**不是 nil，得显式判空。
        var urls = Bundle.main.urls(forResourcesWithExtension: "png", subdirectory: "DefaultStickers") ?? []
        if urls.isEmpty {
            urls = Bundle.main.urls(forResourcesWithExtension: "png", subdirectory: nil) ?? []
        }
        guard !urls.isEmpty else { return }
        for src in urls.sorted(by: { $0.lastPathComponent < $1.lastPathComponent }) {
            guard let data = try? Data(contentsOf: src) else { continue }
            let num = (stickers.map(\.num).max() ?? 0) + 1
            let sticker = Sticker(id: UUID().uuidString,
                                  description: src.deletingPathExtension().lastPathComponent,
                                  num: num)
            do {
                try data.write(to: imageURL(for: sticker), options: .atomic)
                stickers.append(sticker)
            } catch { continue }
        }
        saveManifest()
    }

    // MARK: - 私有

    private func ensureDirectory() {
        if !fileManager.fileExists(atPath: directory.path) {
            try? fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        }
    }

    /// 逐条容错的解码壳：单条坏了只丢那一条，别让整个表情库跟着消失。
    private struct FailableSticker: Decodable {
        let sticker: Sticker?
        init(from decoder: Decoder) throws { sticker = try? Sticker(from: decoder) }
    }

    private func load() {
        guard let data = try? Data(contentsOf: manifestURL),
              let decoded = try? JSONDecoder().decode([FailableSticker].self, from: data) else {
            stickers = []
            return
        }
        // 只保留图片文件确实还在的条目。
        stickers = decoded.compactMap(\.sticker)
            .filter { fileManager.fileExists(atPath: imageURL(for: $0).path) }
    }

    private func saveManifest() {
        do {
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
            let data = try encoder.encode(stickers)
            try data.write(to: manifestURL, options: .atomic)
        } catch {
            print("写入清单失败: \(error)")
        }
    }
}
