import Foundation

/// 当前会话角色（全局唯一入口）。ContentView 的 @AppStorage("currentCharID") 写的就是
/// 这个 key——ChatService 拼请求时从这里读，两边天然对齐，不用把角色一层层传下去。
enum CurrentCharacter {
    static let key = "currentCharID"
    static var id: String {
        UserDefaults.standard.string(forKey: key) ?? "default"
    }
}

/// 一个角色（后端 /characters 的一条）。
struct CharacterInfo: Decodable, Identifiable, Equatable {
    let id: String
    let display_name: String
}

/// 角色清单：从后端拉，本地缓存一份（离线/冷启动时会话列表照样能画）。
@MainActor
final class CharacterListStore: ObservableObject {
    @Published private(set) var items: [CharacterInfo] = []

    private let cacheKey = "characters_cache"
    private let service = ChatService()

    init() {
        if let data = UserDefaults.standard.data(forKey: cacheKey),
           let cached = try? JSONDecoder().decode([CharacterInfo].self, from: data) {
            items = cached
        } else {
            items = [CharacterInfo(id: "default", display_name: "")]
        }
    }

    func refresh() async {
        guard let fresh = try? await service.getCharacters(), !fresh.isEmpty else { return }
        items = fresh
        if let data = try? JSONEncoder().encode(fresh) {
            UserDefaults.standard.set(data, forKey: cacheKey)
        }
    }

    /// 角色的显示名；不认识/为空返回 nil（调用方自己找兜底，比如设置里的 agentName）。
    func name(for id: String) -> String? {
        let n = items.first(where: { $0.id == id })?.display_name ?? ""
        return n.isEmpty ? nil : n
    }
}

// CharacterInfo 要能写进缓存
extension CharacterInfo: Encodable {}
