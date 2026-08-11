import SwiftUI

// MARK: - 数据模型（对齐后端 /mail/drafts）

/// 一封待寄的草稿：TA 想寄给白名单外的人、等机主过目的信。
/// 寄出/删除都在这页；这是发信白名单的唯一例外——人当场看过、人按的键。
struct MailDraft: Decodable, Identifiable {
    let id: String
    let to: String
    let subject: String
    let body: String
    let ts: String                // ISO 时间串（后端 isoformat）
    let origin: String?           // chat / wake（哪条路写下的）

    /// "2026-08-11T15:20:33.…+08:00" → "08-11 15:20"（展示够用，不动时区解析）
    var dateText: String {
        let s = String(ts.prefix(16)).replacingOccurrences(of: "T", with: " ")
        return s.count > 5 ? String(s.dropFirst(5)) : s
    }
}

struct MailDraftsWrap: Decodable {
    let items: [MailDraft]
    let configured: Bool          // 后端 .env 配好邮箱没
    let plugin_installed: Bool    // mail 插件装了没（没装 → 页面引去插件商店）
}

extension ChatService {
    func getMailDrafts() async throws -> MailDraftsWrap {
        let data = try await perform(authedRequest("GET", "/mail/drafts"))
        do { return try JSONDecoder().decode(MailDraftsWrap.self, from: data) }
        catch { throw ChatServiceError.badResponse }
    }

    func sendMailDraft(id: String) async throws {
        // SMTP 真发信，超时放宽
        _ = try await perform(authedRequest("POST", "/mail/drafts/\(id)/send", timeout: 60))
    }

    func deleteMailDraft(id: String) async throws {
        _ = try await perform(authedRequest("POST", "/mail/drafts/\(id)/delete"))
    }
}

// MARK: - 草稿信箱页（抽屉 → push）

/// TA 给白名单外收件人写的信在这排队：过目 → 寄出 / 删除。
struct DraftsPage: View {
    private let service = ChatService()

    @State private var wrap: MailDraftsWrap? = nil
    @State private var loading = true
    @State private var errorText: String? = nil
    @State private var detail: MailDraft? = nil        // 点开看全文的那封
    @State private var sendTarget: MailDraft? = nil    // 确认寄出
    @State private var deleteTarget: MailDraft? = nil  // 确认删除
    @State private var busy = false
    @State private var opError: String? = nil          // 操作失败 alert
    @State private var noteText: String? = nil         // 成功短提示（2.5s 自动消失）
    @State private var noteSeq = 0

    var body: some View {
        Group {
            if loading {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let errorText {
                ContentUnavailableView("读不到草稿信箱", systemImage: "exclamationmark.triangle",
                                       description: Text(errorText))
            } else if let wrap, !wrap.plugin_installed {
                // 没装插件：不摆空列表装样子，直说差什么、给条路过去。
                ContentUnavailableView {
                    Label("还没装邮箱插件", systemImage: "envelope")
                } description: {
                    Text("装上 mail 插件，TA 才有自己的信箱。")
                } actions: {
                    // 直给 destination：value-based 的 navigationDestination 在已 push 的页面里
                    // 不触发（MemoryPage 实测踩过），别改回去。
                    NavigationLink { PluginsPage() } label: { Text("去插件商店") }
                        .buttonStyle(.borderedProminent)
                }
            } else if let wrap, !wrap.configured {
                ContentUnavailableView("邮箱还没配置", systemImage: "envelope.badge.shield.half.filled",
                                       description: Text("在后端 server/.env 里配好 CASSETTE_MAIL_* 再重启后端。"))
            } else if wrap?.items.isEmpty ?? true {
                ContentUnavailableView("没有待寄的信", systemImage: "tray",
                                       description: Text("TA 给白名单外的人写信时，会先放到这里等你过目。"))
            } else {
                List {
                    Section {
                        ForEach(wrap?.items ?? []) { d in row(d) }
                    } footer: {
                        Text("白名单外的收件人 TA 自己寄不出去——这里点「寄出」才真发。寄出就收不回。")
                    }
                }
                .listStyle(.insetGrouped)
            }
        }
        .background(Color(.systemGroupedBackground))
        .navigationTitle("草稿信箱")
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { await load() }
        .task { await load() }
        .sheet(item: $detail) { d in detailSheet(d) }
        .overlay(alignment: .bottom) {
            if let noteText {
                Text(noteText)
                    .font(.footnote)
                    .padding(.horizontal, 14).padding(.vertical, 9)
                    .background(.ultraThinMaterial, in: Capsule())
                    .overlay(Capsule().stroke(.separator))
                    .padding(.bottom, 24)
                    .transition(.opacity.combined(with: .move(edge: .bottom)))
            }
        }
        .animation(.easeInOut(duration: 0.2), value: noteText)
        .alert("操作失败", isPresented: Binding(
            get: { opError != nil }, set: { if !$0 { opError = nil } }
        )) { Button("好", role: .cancel) { } } message: { Text(opError ?? "") }
        .confirmationDialog("把这封信寄给 \(sendTarget?.to ?? "")？寄出就收不回。",
                            isPresented: Binding(get: { sendTarget != nil },
                                                 set: { if !$0 { sendTarget = nil } }),
                            titleVisibility: .visible) {
            Button("寄出") { if let d = sendTarget { Task { await send(d) } } }
            Button("取消", role: .cancel) { }
        }
        .confirmationDialog("删除这封草稿？TA 写的内容会丢。",
                            isPresented: Binding(get: { deleteTarget != nil },
                                                 set: { if !$0 { deleteTarget = nil } }),
                            titleVisibility: .visible) {
            Button("删除", role: .destructive) { if let d = deleteTarget { Task { await remove(d) } } }
            Button("取消", role: .cancel) { }
        }
    }

    // MARK: 行 / 详情

    @ViewBuilder
    private func row(_ d: MailDraft) -> some View {
        Button { detail = d } label: {
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text(d.to).font(.subheadline.bold()).foregroundStyle(.primary)
                    Spacer()
                    Text(d.dateText).font(.caption).foregroundStyle(.secondary)
                }
                Text(d.subject.isEmpty ? "（无主题）" : d.subject)
                    .font(.subheadline).foregroundStyle(.primary)
                Text(d.body)
                    .font(.footnote).foregroundStyle(.secondary).lineLimit(2)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .swipeActions(edge: .trailing) {
            Button(role: .destructive) { deleteTarget = d } label: {
                Label("删除", systemImage: "trash")
            }
            .tint(.red)   // 全 app 的 .tint(Color.theme) 会盖掉破坏性默认红，显式压回红
        }
    }

    @ViewBuilder
    private func detailSheet(_ d: MailDraft) -> some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    LabeledContent("收件人") { Text(d.to).textSelection(.enabled) }
                    LabeledContent("时间") { Text(d.dateText) }
                    Divider()
                    Text(d.body)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .padding()
            }
            .navigationTitle(d.subject.isEmpty ? "（无主题）" : d.subject)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("关闭") { detail = nil }
                }
            }
            .safeAreaInset(edge: .bottom) {
                HStack(spacing: 12) {
                    Button {
                        deleteTarget = d
                    } label: {
                        Text("删除").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.bordered)
                    .tint(.red)
                    Button {
                        sendTarget = d
                    } label: {
                        Group { busy ? AnyView(ProgressView()) : AnyView(Text("寄出")) }
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(busy)
                }
                .padding()
                .background(.bar)
            }
        }
    }

    // MARK: 动作

    private func load() async {
        errorText = nil
        do { wrap = try await service.getMailDrafts() }
        catch { errorText = (error as? ChatServiceError)?.errorDescription ?? "连不上后端" }
        loading = false
    }

    private func send(_ d: MailDraft) async {
        busy = true
        do {
            try await service.sendMailDraft(id: d.id)
            detail = nil
            showNote("已寄出，收不回啦")
        } catch {
            opError = (error as? ChatServiceError)?.errorDescription ?? "寄出失败"
        }
        busy = false
        await load()
    }

    private func remove(_ d: MailDraft) async {
        do {
            try await service.deleteMailDraft(id: d.id)
            detail = nil
            showNote("已删除")
        } catch {
            opError = (error as? ChatServiceError)?.errorDescription ?? "删除失败"
        }
        await load()
    }

    private func showNote(_ text: String) {
        noteSeq += 1
        let seq = noteSeq
        noteText = text
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) {
            if noteSeq == seq { noteText = nil }
        }
    }
}
