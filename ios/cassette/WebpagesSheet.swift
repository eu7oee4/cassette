import SwiftUI
import WebKit

// MARK: - 数据模型（对齐后端 /webpages，产物来自 webpage 插件）

struct WebpageItem: Decodable, Identifiable {
    let id: String
    let title: String
    let ts: Int
}

struct WebpageDetail: Decodable {
    let id: String
    let title: String
    let html: String
}

extension ChatService {
    func getWebpages() async throws -> [WebpageItem] {
        let data = try await perform(authedRequest("GET", "/webpages"))
        struct Wrap: Decodable { let items: [WebpageItem] }
        do { return try JSONDecoder().decode(Wrap.self, from: data).items }
        catch { throw ChatServiceError.badResponse }
    }

    func getWebpage(id: String) async throws -> WebpageDetail {
        let data = try await perform(authedRequest("GET", "/webpages/\(id)"))
        do { return try JSONDecoder().decode(WebpageDetail.self, from: data) }
        catch { throw ChatServiceError.badResponse }
    }

    func deleteWebpage(id: String) async throws {
        _ = try await perform(authedRequest("POST", "/webpages/\(id)/delete",
                                            jsonBody: Data("{}".utf8)))
    }
}

// MARK: - HTML 文件列表（聊天记录页入口的 sheet）

struct WebpagesSheet: View {
    private let service = ChatService()

    @State private var items: [WebpageItem] = []
    @State private var loading = true
    @State private var opened: WebpageItem? = nil

    var body: some View {
        NavigationStack {
            Group {
                if loading {
                    ProgressView()
                } else if items.isEmpty {
                    ContentUnavailableView("还没有网页", systemImage: "doc.richtext",
                                           description: Text("装上网页插件后，TA 就能做页面给你了。"))
                } else {
                    List {
                        ForEach(items) { p in
                            Button { opened = p } label: {
                                HStack {
                                    Image(systemName: "doc.richtext")
                                        .foregroundStyle(Color.theme)
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(p.title).foregroundStyle(.primary).lineLimit(1)
                                        Text(Self.fmt.string(from: Date(timeIntervalSince1970: TimeInterval(p.ts))))
                                            .font(.caption).foregroundStyle(.secondary)
                                    }
                                }
                            }
                            .swipeActions(edge: .trailing) {
                                Button(role: .destructive) {
                                    Task { try? await service.deleteWebpage(id: p.id); await load() }
                                } label: { Label("删除", systemImage: "trash") }
                            }
                        }
                    }
                    .listStyle(.plain)
                }
            }
            .navigationTitle("HTML 文件")
            .navigationBarTitleDisplayMode(.inline)
            .refreshable { await load() }
            .task { await load() }
            .sheet(item: $opened) { p in WebpageViewerSheet(item: p) }
        }
        .presentationDetents([.medium, .large])
    }

    private func load() async {
        items = (try? await service.getWebpages()) ?? []
        loading = false
    }

    static let fmt: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd HH:mm"
        return f
    }()
}

// MARK: - 网页查看

private struct WebpageViewerSheet: View {
    let item: WebpageItem
    private let service = ChatService()
    @State private var html: String? = nil

    var body: some View {
        NavigationStack {
            Group {
                if let html {
                    HTMLView(html: html)
                } else {
                    ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            }
            .navigationTitle(item.title)
            .navigationBarTitleDisplayMode(.inline)
            .task { html = (try? await service.getWebpage(id: item.id))?.html }
        }
    }
}

/// WKWebView 壳：渲染自包含 HTML 串。页面是 TA 自己写的本地产物，不开 JS 之外的能力。
private struct HTMLView: UIViewRepresentable {
    let html: String

    func makeUIView(context: Context) -> WKWebView {
        WKWebView()
    }

    func updateUIView(_ webView: WKWebView, context: Context) {
        webView.loadHTMLString(html, baseURL: nil)
    }
}
