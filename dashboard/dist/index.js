/**
 * Hermes Notes — Dashboard Plugin (v3)
 *
 * 核心修正：
 *  - 使用 SDK.fetchJSON（自动带 session token + 自动 parse JSON）
 *  - 不使用 authedFetch（它只返回 raw Response，不 parse）
 *  - 不引入多余 hooks 调用
 */
(function () {
  "use strict";

  var SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) return;

  var React = SDK.React;
  var h = React.createElement;
  var f = SDK.fetchJSON; // kanban 同款 —— 自动带 token + parse JSON + throw on error
  var Card = SDK.components.Card;
  var CardContent = SDK.components.CardContent;
  var Badge = SDK.components.Badge;
  var Button = SDK.components.Button;
  var Input = SDK.components.Input;

  var API = "/api/plugins/sqlite-note-store";

  // ── 小工具 ────────────────────────────────────────────────────────────

  function ago(ts) {
    if (!ts) return "—";
    var sec = typeof ts === "string" ? Math.floor(new Date(ts).getTime() / 1000) : ts;
    var d = Math.floor(Date.now() / 1000 - sec);
    if (d < 60) return d + "s";
    if (d < 3600) return Math.floor(d / 60) + "m";
    if (d < 86400) return Math.floor(d / 3600) + "h";
    return Math.floor(d / 86400) + "d";
  }


  // ── INDEX 树 ──────────────────────────────────────────────────────────

  function IndexTree(props) {
    var s = React.useState({ data: null, loading: true, error: null, expanded: {} });
    var st = s[0], setSt = s[1];
    var refreshKey = props.refreshKey;

    React.useEffect(function () {
      setSt(function (p) { return Object.assign({}, p, { loading: true }); });
      f(API + "/index")
        .then(function (r) { setSt(function (p) { return Object.assign({}, p, { data: r, loading: false, error: null }); }); })
        .catch(function (e) { setSt(function (p) { return Object.assign({}, p, { loading: false, error: String(e.message || e) }); }); });
    }, [refreshKey]);

    var toggle = React.useCallback(function (key) {
      setSt(function (prev) {
        var n = Object.assign({}, prev.expanded || {});
        n[key] = !n[key];
        return Object.assign({}, prev, { expanded: n });
      });
    }, []);

    if (st.loading) return h("div", { style: { padding: "16px", color: "#888", fontSize: "13px" } }, "加载中…");
    if (st.error) return h("div", { style: { padding: "16px", color: "#e53e3e", fontSize: "13px" } }, "错误: " + st.error);
    if (!st.data || !st.data.categories || st.data.categories.length === 0) {
      // 后端 /index 返回裸数组 [{category, files}, ...]，非 {categories: [...]}
      var cats = Array.isArray(st.data) ? st.data : (st.data && st.data.categories);
      if (!cats || cats.length === 0)
        return h("div", { style: { padding: "16px", color: "#888", fontSize: "13px" } }, "记忆库为空");
      st.data = { categories: cats };
    }

    var expanded = st.expanded || {};
    var tree = [];
    var sel = props.selectedNode;

    // 扁平 categories → 嵌套树（按 "/" 分段；父节点展开时显示子分类 + 直接组）
    function buildCatTree(cats) {
      var roots = [], byPath = {};
      cats.forEach(function (cat) {
        var segs = (cat.category || "").split("/").filter(Boolean);
        if (segs.length === 0) segs = ["uncategorized"];
        var parent = null, parentPath = "";
        for (var i = 0; i < segs.length; i++) {
          var nodePath = parentPath ? parentPath + "/" + segs[i] : segs[i];
          var node = byPath[nodePath];
          if (!node) {
            node = { name: segs[i], path: nodePath, cat: null, children: [], files: [] };
            byPath[nodePath] = node;
            if (parent) parent.children.push(node); else roots.push(node);
          }
          parent = node; parentPath = nodePath;
        }
        parent.cat = cat;
        parent.files = cat.files || [];
      });
      return roots;
    }

    // 每节点的子树条目数（含子分类），父分类行显示汇总
    var nodeCounts = {};
    function countNode(node) {
      var n = 0;
      (node.files || []).forEach(function (fl) { n += (fl.entry_count || (fl.entries ? fl.entries.length : 0)); });
      (node.children || []).forEach(function (ch) { n += countNode(ch); });
      nodeCounts[node.path] = n;
      return n;
    }

    function renderCatNode(node, depth) {
      var catKey = "cat:" + node.path;
      var isCatOpen = !!expanded[catKey];
      var isCatActive = sel && sel.type === "category" && sel.name === node.path;
      var catCount = nodeCounts[node.path] || 0;
      var directFiles = (node.cat && node.cat.files) ? node.cat.files : [];

      tree.push(h("div", {
        key: catKey,
        style: {
          display: "flex", alignItems: "center", gap: "4px", padding: "4px 8px", cursor: "pointer", fontSize: "13px", fontWeight: 500, borderRadius: "4px",
          paddingLeft: (8 + depth * 14) + "px",
          background: isCatActive ? "rgba(59,130,246,0.15)" : "transparent",
        },
        onClick: function () {
          toggle(catKey);
          if (props.onSelectNode) props.onSelectNode({ type: "category", name: node.path, fileCount: directFiles.length });
        },
      },
        h("span", { style: { fontSize: "10px", color: "#888", width: "12px" } }, isCatOpen ? "▼" : "▶"),
        h("span", { style: isCatActive ? { color: "#3b82f6" } : {} }, node.name || "(未分类)"),
        h("span", { style: { fontSize: "10px", color: "#888", marginLeft: "4px" } }, String(catCount))
      ));

      if (isCatOpen) {
        node.children.forEach(function (ch) { renderCatNode(ch, depth + 1); });
        directFiles.forEach(function (file) {
          var fileKey = "file:" + node.path + ":" + file.id;
          var isFileOpen = !!expanded[fileKey];
          var isFileActive = sel && sel.type === "file" && sel.id === file.id;

          tree.push(h("div", {
            key: fileKey,
            style: {
              display: "flex", alignItems: "center", gap: "4px", padding: "4px 16px", cursor: "pointer", fontSize: "13px", borderRadius: "4px",
              paddingLeft: (16 + (depth + 1) * 14) + "px",
              background: isFileActive ? "rgba(59,130,246,0.15)" : "transparent",
            },
            onClick: function () {
              toggle(fileKey);
              if (props.onSelectNode) props.onSelectNode({ type: "file", id: file.id, title: file.title || file.path, category: node.path, dirty: !!file.dirty, entryCount: file.entries ? file.entries.length : (file.entry_count || 0) });
            },
          },
            h("span", { style: { fontSize: "10px", color: "#888", width: "12px" } }, isFileOpen ? "▼" : "▶"),
            h("span", { style: isFileActive ? { color: "#3b82f6" } : (file.dirty ? { color: "#e67e22" } : {}) }, file.title || file.path),
            file.dirty && h("span", { style: { fontSize: "9px", color: "#e67e22", marginLeft: "4px", border: "1px solid #e67e22", borderRadius: "3px", padding: "0 3px" } }, "dirty")
          ));

          if (isFileOpen && file.entries) {
            file.entries.forEach(function (entry) {
              var isEntryActive = sel && sel.type === "entry" && sel.id === entry.id;
              tree.push(h("div", {
                key: "entry:" + entry.id,
                style: {
                  padding: "3px 8px 3px 32px", cursor: "pointer", fontSize: "12px", borderRadius: "4px", marginLeft: "20px",
                  paddingLeft: (32 + depth * 14) + "px",
                  background: isEntryActive ? "rgba(59,130,246,0.15)" : "transparent",
                  color: isEntryActive ? "#3b82f6" : "inherit",
                },
                onClick: function () { if (props.onSelectEntry) props.onSelectEntry(entry.id); },
              }, entry.header || ("#" + entry.id)));
            });
          }
        });
      }
    }

    var roots = buildCatTree(st.data.categories);
    roots.forEach(countNode);
    roots.forEach(function (root) { renderCatNode(root, 0); });

    return h("div", { style: { overflowY: "auto", flex: 1 } }, tree);
  }

  // ── 条目编辑器 ────────────────────────────────────────────────────────

  function EntryEditor(props) {
    var s = React.useState({ data: null, loading: false, error: null, saving: false, dirty: false,
                              editHeader: "", editBody: "" });
    var st = s[0], setSt = s[1];
    var id = props.entryId;

    React.useEffect(function () {
      if (!id) { setSt(function (p) { return Object.assign({}, p, { data: null, loading: false }); }); return; }
      setSt(function (p) { return Object.assign({}, p, { loading: true, error: null }); });
      f(API + "/entries/" + id)
        .then(function (r) { setSt(function (p) { return Object.assign({}, p, { data: r, loading: false, error: null,
          editHeader: r.header || "", editBody: r.content || "" }); }); })
        .catch(function (e) { setSt(function (p) { return Object.assign({}, p, { loading: false, error: String(e.message || e) }); }); });
    }, [id]);

    if (!id) return h("div", { style: { padding: "24px", color: "#888", fontSize: "13px", textAlign: "center" } }, "← 从左侧选择一个条目");
    if (st.loading) return h("div", { style: { padding: "16px", color: "#888", fontSize: "13px" } }, "加载中…");
    if (st.error) return h("div", { style: { padding: "16px", color: "#e53e3e", fontSize: "13px" } }, "错误: " + st.error);
    if (!st.data) return null;

    function save() {
      setSt(function (p) { return Object.assign({}, p, { saving: true, error: null }); });
      var body = JSON.stringify({ header: st.editHeader, content: st.editBody });
      f(API + "/entries/" + id, { method: "PUT", body: body, headers: { "Content-Type": "application/json" } })
        .then(function (r) { setSt(function (p) { return Object.assign({}, p, { saving: false, dirty: false }); });
          if (props.onSaved) props.onSaved();
        })
        .catch(function (e) { setSt(function (p) { return Object.assign({}, p, { saving: false, error: String(e.message || e) }); }); });
    }

    function del() {
      if (!confirm("确认删除此条目？")) return;
      f(API + "/entries/" + id, { method: "DELETE" })
        .then(function () { if (props.onDeleted) props.onDeleted(); })
        .catch(function (e) { setSt(function (p) { return Object.assign({}, p, { error: String(e.message || e) }); }); });
    }

    var d = st.data;

    return h("div", { style: { display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" } },
      // header bar
      h("div", { style: { display: "flex", alignItems: "center", gap: "8px", padding: "8px 12px", borderBottom: "1px solid #222" } },
        h("span", { style: { fontSize: "12px", color: "#888" } }, "#" + d.id),
        h(Input, { value: st.editHeader, onChange: function (e) { setSt(function (p) { return Object.assign({}, p, { editHeader: e.target.value, dirty: true }); }); },
          style: { flex: 1, fontSize: "13px" } }),
        h(Badge, { tone: st.dirty ? "warning" : "outline" }, st.dirty ? "未保存" : "已保存"),
        h(Button, { size: "sm", onClick: save, disabled: st.saving || !st.dirty }, st.saving ? "保存中…" : "保存"),
        h(Button, { size: "sm", outlined: true, onClick: del, style: { color: "#e53e3e" } }, "删除")
      ),
      // metadata bar
      h("div", { style: { display: "flex", gap: "12px", padding: "4px 12px", fontSize: "11px", color: "#666", borderBottom: "1px solid #222" } },
        h("span", null, "分组: " + (d.file_title || d.file_path || "—")),
        h("span", null, "分类: " + (d.category || "—")),
        h("span", null, "最后使用: " + ago(d.last_used)),
        d.dirty && h("span", { style: { color: "#e67e22" } }, "· dirty")
      ),
      // body textarea
      h("textarea", {
        value: st.editBody,
        onChange: function (e) { setSt(function (p) { return Object.assign({}, p, { editBody: e.target.value, dirty: true }); }); },
        style: { flex: 1, border: "none", outline: "none", padding: "12px", fontFamily: "monospace", fontSize: "13px",
          resize: "none", background: "transparent", color: "inherit", lineHeight: "1.6" }
      })
    );
  }

  // ── 分组编辑器 ──────────────────────────────────────────────────────────

  function FileEditor(props) {
    var s = React.useState({ title: props.title || "", category: props.category || "", saving: false, error: null, delError: null });
    var st = s[0], setSt = s[1];

    React.useEffect(function () {
      setSt(function (p) { return Object.assign({}, p, { title: props.title || "", category: props.category || "", error: null, delError: null }); });
    }, [props.id, props.title, props.category]);

    function save() {
      if (!st.title.trim()) { setSt(function (p) { return Object.assign({}, p, { error: "标题不能为空" }); }); return; }
      setSt(function (p) { return Object.assign({}, p, { saving: true, error: null }); });
      f(API + "/files/" + props.id, { method: "PUT", body: JSON.stringify({ title: st.title.trim(), category: st.category.trim() }) })
        .then(function () { if (props.onSaved) props.onSaved(); })
        .catch(function (e) { setSt(function (p) { return Object.assign({}, p, { saving: false, error: String(e.message || e) }); }); });
    }

    function del() {
      if (!confirm("确认删除此分组？")) return;
      setSt(function (p) { return Object.assign({}, p, { delError: null }); });
      f(API + "/files/" + props.id, { method: "DELETE" })
        .then(function () {
          if (props.onDeleted) props.onDeleted();
        })
        .catch(function (e) {
          // 非空分组返回 409
          var msg = e.message || String(e);
          if (msg.indexOf("409") >= 0) msg = "分组非空，请先清空条目后再删除";
          setSt(function (p) { return Object.assign({}, p, { delError: msg }); });
        });
    }

    return h("div", { style: { display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" } },
      h("div", { style: { display: "flex", alignItems: "center", gap: "8px", padding: "8px 12px", borderBottom: "1px solid #222" } },
        h("span", { style: { fontSize: "12px", color: "#888" } }, "分组 #" + props.id),
        h("span", { style: { flex: 1, fontSize: "13px", color: "#ccc", fontWeight: 600 } }, props.title || props.path),
        props.dirty && h("span", { style: { fontSize: "9px", color: "#e67e22", border: "1px solid #e67e22", borderRadius: "3px", padding: "0 4px" } }, "dirty")
      ),
      h("div", { style: { flex: 1, overflowY: "auto", padding: "16px" } },
        h("div", { style: { marginBottom: "12px" } },
          h("label", { style: { display: "block", fontSize: "11px", color: "#888", marginBottom: "4px" } }, "分组标题"),
          h(Input, { value: st.title, onChange: function (e) { setSt(function (p) { return Object.assign({}, p, { title: e.target.value }); }); }, placeholder: "分组标题" })
        ),
        h("div", { style: { marginBottom: "12px" } },
          h("label", { style: { display: "block", fontSize: "11px", color: "#888", marginBottom: "4px" } }, "分类"),
          h(Input, { value: st.category, onChange: function (e) { setSt(function (p) { return Object.assign({}, p, { category: e.target.value }); }); }, placeholder: "分类名（文件夹名）" })
        ),
        st.error && h("div", { style: { color: "#e53e3e", fontSize: "12px", marginBottom: "8px" } }, st.error),
        st.delError && h("div", { style: { color: "#e53e3e", fontSize: "12px", marginBottom: "8px" } }, st.delError)
      ),
      h("div", { style: { display: "flex", gap: "8px", padding: "8px 12px", borderTop: "1px solid #222" } },
        h(Button, { size: "sm", variant: "primary", disabled: st.saving, onClick: save }, st.saving ? "保存中…" : "保存"),
        h(Button, { size: "sm", variant: "danger", onClick: del }, "删除分组"),
        h("span", { style: { fontSize: "11px", color: "#888", alignSelf: "center" } }, props.entryCount + " 个条目")
      )
    );
  }

  // ── 分类编辑器 ──────────────────────────────────────────────────────────

  function CategoryEditor(props) {
    var s = React.useState({ newName: props.name || "", saving: false, error: null, delError: null });
    var st = s[0], setSt = s[1];

    React.useEffect(function () {
      setSt(function (p) { return Object.assign({}, p, { newName: props.name || "", error: null, delError: null }); });
    }, [props.name]);

    function save() {
      if (!st.newName.trim()) { setSt(function (p) { return Object.assign({}, p, { error: "分类名不能为空" }); }); return; }
      if (st.newName.trim() === props.name) { setSt(function (p) { return Object.assign({}, p, { error: "分类名未变更" }); }); return; }
      setSt(function (p) { return Object.assign({}, p, { saving: true, error: null }); });
      f(API + "/categories", { method: "PUT", body: JSON.stringify({ old_name: props.name, new_name: st.newName.trim() }) })
        .then(function () { if (props.onSaved) props.onSaved(); })
        .catch(function (e) { setSt(function (p) { return Object.assign({}, p, { saving: false, error: String(e.message || e) }); }); });
    }

    function del() {
      if (!confirm("确认删除此分类？")) return;
      setSt(function (p) { return Object.assign({}, p, { delError: null }); });
      f(API + "/categories/" + encodeURIComponent(props.name), { method: "DELETE" })
        .then(function () {
          if (props.onDeleted) props.onDeleted();
        })
        .catch(function (e) {
          var msg = e.message || String(e);
          if (msg.indexOf("409") >= 0) msg = "分类非空，请先清空分组后再删除";
          setSt(function (p) { return Object.assign({}, p, { delError: msg }); });
        });
    }

    return h("div", { style: { display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" } },
      h("div", { style: { display: "flex", alignItems: "center", gap: "8px", padding: "8px 12px", borderBottom: "1px solid #222" } },
        h("span", { style: { fontSize: "12px", color: "#888" } }, "分类"),
        h("span", { style: { flex: 1, fontSize: "13px", color: "#ccc", fontWeight: 600 } }, props.name)
      ),
      h("div", { style: { flex: 1, overflowY: "auto", padding: "16px" } },
        h("div", { style: { marginBottom: "12px" } },
          h("label", { style: { display: "block", fontSize: "11px", color: "#888", marginBottom: "4px" } }, "分类名"),
          h(Input, { value: st.newName, onChange: function (e) { setSt(function (p) { return Object.assign({}, p, { newName: e.target.value }); }); }, placeholder: "新分类名" })
        ),
        st.error && h("div", { style: { color: "#e53e3e", fontSize: "12px", marginBottom: "8px" } }, st.error),
        st.delError && h("div", { style: { color: "#e53e3e", fontSize: "12px", marginBottom: "8px" } }, st.delError)
      ),
      h("div", { style: { display: "flex", gap: "8px", padding: "8px 12px", borderTop: "1px solid #222" } },
        h(Button, { size: "sm", variant: "primary", disabled: st.saving, onClick: save }, st.saving ? "保存中…" : "重命名"),
        h(Button, { size: "sm", variant: "danger", onClick: del }, "删除分类"),
        h("span", { style: { fontSize: "11px", color: "#888", alignSelf: "center" } }, props.fileCount + " 个分组")
      )
    );
  }

  // ── 搜索面板 ──────────────────────────────────────────────────────────

  function SearchPanel(props) {
    var s = React.useState({ q: "", results: null, loading: false, error: null });
    var st = s[0], setSt = s[1];

    var doSearch = React.useCallback(function (q) {
      if (!q || q.trim().length < 1) { setSt(function (p) { return Object.assign({}, p, { results: null }); }); return; }
      setSt(function (p) { return Object.assign({}, p, { loading: true, error: null }); });
      var url = API + "/search?q=" + encodeURIComponent(q.trim());
      f(url)
        .then(function (r) { setSt(function (p) { return Object.assign({}, p, { results: r, loading: false }); }); })
        .catch(function (e) { setSt(function (p) { return Object.assign({}, p, { loading: false, error: String(e.message || e) }); }); });
    }, []);

    var results = (st.results && st.results.results) || [];

    return h("div", { style: { display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" } },
      h("div", { style: { padding: "8px", borderBottom: "1px solid #222" } },
        h(Input, {
          placeholder: "搜索条目标题或内容…",
          value: st.q,
          onChange: function (e) {
            var v = e.target.value;
            setSt(function (p) { return Object.assign({}, p, { q: v }); });
            clearTimeout(window._notes_search_timer);
            window._notes_search_timer = setTimeout(function () { doSearch(v); }, 300);
          },
          style: { width: "100%", fontSize: "13px" }
        })
      ),
      st.loading && h("div", { style: { padding: "16px", color: "#888", fontSize: "13px" } }, "搜索中…"),
      st.error && h("div", { style: { padding: "16px", color: "#e53e3e", fontSize: "13px" } }, "错误: " + st.error),
      results.length > 0 && h("div", { style: { overflowY: "auto", flex: 1 } },
        results.map(function (r) {
          return h("div", {
            key: r.id,
            style: { padding: "8px 12px", cursor: "pointer", borderBottom: "1px solid #1a1a1a", fontSize: "13px" },
            onClick: function () { props.onSelectEntry(r.id); },
          },
            h("div", { style: { fontWeight: 500, marginBottom: "2px" } }, r.header || ("#" + r.id)),
            h("div", { style: { fontSize: "11px", color: "#888" } },
              (r.snippet || "").substring(0, 80) + ((r.snippet || "").length > 80 ? "…" : ""))
          );
        })
      ),
      !st.loading && st.results && results.length === 0 && h("div", { style: { padding: "16px", color: "#888", fontSize: "13px" } }, "无结果")
    );
  }

  // ── 冷存储面板 ────────────────────────────────────────────────────────

  function ColdPanel(props) {
    var s = React.useState({ data: null, loading: true, error: null });
    var st = s[0], setSt = s[1];

    React.useEffect(function () {
      f(API + "/cold")
        .then(function (r) { setSt(function (p) { return Object.assign({}, p, { data: r, loading: false }); }); })
        .catch(function (e) { setSt(function (p) { return Object.assign({}, p, { loading: false, error: String(e.message || e) }); }); });
    }, []);

    if (st.loading) return h("div", { style: { padding: "16px", color: "#888", fontSize: "13px" } }, "加载中…");
    if (st.error) return h("div", { style: { padding: "16px", color: "#e53e3e", fontSize: "13px" } }, "错误: " + st.error);

    var files = (st.data && st.data.files) || [];

    if (files.length === 0) return h("div", { style: { padding: "16px", color: "#888", fontSize: "13px" } }, "冷存储为空");

    return h("div", { style: { overflowY: "auto", flex: 1 } },
      h("div", { style: { padding: "8px 12px", fontSize: "11px", color: "#666" } }, "共 " + files.length + " 个冷存储批次"),
      files.map(function (file) {
        return h("div", {
          key: file.id,
          style: { padding: "8px 12px", borderBottom: "1px solid #1a1a1a", fontSize: "13px" },
        },
          h("div", { style: { fontWeight: 500, marginBottom: "2px" } }, file.filename || file.title || file.path),
          h("div", { style: { fontSize: "11px", color: "#888" } },
            (file.entries ? file.entries.length : 0) + " 条目 · 冷入 " + ago(file.created))
        );
      })
    );
  }

  // ── 统计面板 ──────────────────────────────────────────────────────────

  function StatsPanel() {
    var s = React.useState({ data: null, loading: true, error: null });
    var st = s[0], setSt = s[1];

    React.useEffect(function () {
      f(API + "/stats")
        .then(function (r) { setSt(function (p) { return Object.assign({}, p, { data: r, loading: false }); }); })
        .catch(function (e) { setSt(function (p) { return Object.assign({}, p, { loading: false, error: String(e.message || e) }); }); });
    }, []);

    if (st.loading) return h("div", { style: { padding: "16px", color: "#888", fontSize: "13px" } }, "加载中…");
    if (st.error) return h("div", { style: { padding: "16px", color: "#e53e3e", fontSize: "13px" } }, "错误: " + st.error);
    if (!st.data) return null;

    var d = st.data;

    return h("div", { style: { padding: "16px", display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(160px, 1fr))", gap: "12px" } },
      statCard("活跃分组", d.total_groups),
      statCard("活跃条目", d.total_entries),
      statCard("Dirty 分组", d.dirty_groups),
      statCard("冷存储批次", d.cold_batches),
      statCard("冷存储条目", d.cold_entries),
      statCard("分类数", d.categories ? d.categories.length : 0)
    );
  }

  function statCard(label, val) {
    return h(Card, { style: { minWidth: "140px" } },
      h(CardContent, { style: { padding: "16px" } },
        h("div", { style: { fontSize: "28px", fontWeight: 700 } }, String(val || 0)),
        h("div", { style: { fontSize: "12px", color: "#888", marginTop: "4px" } }, label)
      )
    );
  }

  // ── 新建条目弹窗 ──────────────────────────────────────────────────────

  function NewEntryModal(props) {
    var s = React.useState({ mode: "existing", header: "", body: "", fileId: 0, files: [], loading: true,
      newCategory: "", existingCategories: [], newTitle: "", saving: false, error: null });
    var st = s[0], setSt = s[1];

    React.useEffect(function () {
      Promise.all([f(API + "/index"), f(API + "/categories")])
        .then(function (results) {
          var data = results[0], catsData = results[1];
          var cats = Array.isArray(data) ? data : (data.categories || []);
          var opts = [];
          cats.forEach(function (c) {
            (c.files || []).forEach(function (fl) {
              opts.push({ id: fl.id, label: c.category + " / " + (fl.title || fl.path || "#" + fl.id) });
            });
          });
          var defaultId = props.selectedFileId || (opts.length > 0 ? opts[0].id : 0);
          setSt(function (p) { return Object.assign({}, p, {
            files: opts, fileId: defaultId, loading: false,
            existingCategories: catsData.categories || []
          }); });
        })
        .catch(function (e) { setSt(function (p) { return Object.assign({}, p, { loading: false, error: "加载失败: " + String(e.message || e) }); }); });
    }, []);

    function create() {
      if (!st.header.trim()) { setSt(function (p) { return Object.assign({}, p, { error: "标题不能为空" }); }); return; }
      setSt(function (p) { return Object.assign({}, p, { saving: true, error: null }); });

      if (st.mode === "new") {
        // 先创建分组，再创建条目
        if (!st.newTitle.trim()) { setSt(function (p) { return Object.assign({}, p, { saving: false, error: "新分组标题不能为空" }); }); return; }
        f(API + "/files", { method: "POST", body: JSON.stringify({ category: st.newCategory.trim() || "uncategorized", title: st.newTitle.trim() }),
          headers: { "Content-Type": "application/json" } })
          .then(function (fr) {
            return f(API + "/entries", { method: "POST", body: JSON.stringify({ header: st.header, content: st.body, file_id: fr.id }),
              headers: { "Content-Type": "application/json" } });
          })
          .then(function (r) { if (props.onCreated) props.onCreated(r.id); })
          .catch(function (e) { setSt(function (p) { return Object.assign({}, p, { saving: false, error: String(e.message || e) }); }); });
      } else {
        if (!st.fileId) { setSt(function (p) { return Object.assign({}, p, { saving: false, error: "请先选择目标分组" }); }); return; }
        f(API + "/entries", { method: "POST", body: JSON.stringify({ header: st.header, content: st.body, file_id: st.fileId }),
          headers: { "Content-Type": "application/json" } })
          .then(function (r) { if (props.onCreated) props.onCreated(r.id); })
          .catch(function (e) { setSt(function (p) { return Object.assign({}, p, { saving: false, error: String(e.message || e) }); }); });
      }
    }

    var modalInput = { background: "#111", color: "#ccc", border: "1px solid #333", borderRadius: "4px", padding: "8px", fontSize: "13px", fontFamily: "inherit" };
    var modeBtn = function (active) { return { flex: 1, padding: "6px", fontSize: "12px", cursor: "pointer", border: "1px solid",
      borderColor: active ? "#3b82f6" : "#333", background: active ? "rgba(59,130,246,0.15)" : "transparent",
      color: active ? "#3b82f6" : "#888", borderRadius: "4px" }; };

    return h("div", { style: { position: "fixed", top: 0, left: 0, right: 0, bottom: 0, background: "rgba(0,0,0,0.6)", zIndex: 9999,
        display: "flex", alignItems: "center", justifyContent: "center" } },
      h("div", { style: { background: "#1a1a2e", border: "1px solid #333", borderRadius: "8px", padding: "20px", width: "520px", maxWidth: "90vw", position: "relative" } },
        h("button", {
          onClick: props.onClose,
          style: { position: "absolute", top: "10px", right: "12px", background: "none", border: "none", color: "#888",
            fontSize: "18px", cursor: "pointer", padding: "0", lineHeight: "1", width: "24px", height: "24px", display: "flex", alignItems: "center", justifyContent: "center", borderRadius: "4px" }
        }, "✕"),
        h("h3", { style: { fontSize: "15px", fontWeight: 600, marginBottom: "16px", paddingRight: "30px" } }, "新建条目"),
        // 模式切换
        h("div", { style: { display: "flex", gap: "8px", marginBottom: "12px" } },
          h("button", { style: modeBtn(st.mode === "existing"), onClick: function () { setSt(function (p) { return Object.assign({}, p, { mode: "existing" }); }); } }, "选择已有分组"),
          h("button", { style: modeBtn(st.mode === "new"), onClick: function () { setSt(function (p) { return Object.assign({}, p, { mode: "new" }); }); } }, "新建分组")
        ),
        h("div", { style: { display: "flex", flexDirection: "column", gap: "12px" } },
          st.loading
            ? h("div", { style: { color: "#888", fontSize: "13px" } }, "加载分组列表…")
            : st.mode === "existing"
              ? (st.files.length === 0
                  ? h("div", { style: { color: "#e53e3e", fontSize: "13px" } }, "记忆库中没有分组，请切换到「新建分组」模式。")
                  : h("select", {
                      value: st.fileId,
                      onChange: function (e) { setSt(function (p) { return Object.assign({}, p, { fileId: parseInt(e.target.value, 10) }); }); },
                      style: modalInput
                    },
                      st.files.map(function (opt) { return h("option", { key: opt.id, value: opt.id }, opt.label); })
                    ))
              : // new file mode
                h("div", { style: { display: "flex", flexDirection: "column", gap: "8px" } },
                  h("div", null,
                    h("label", { style: { fontSize: "11px", color: "#888", display: "block", marginBottom: "4px" } }, "分类"),
                    h("input", {
                      type: "text",
                      list: "cat-list",
                      placeholder: "输入或选择分类名",
                      value: st.newCategory,
                      onChange: function (e) { setSt(function (p) { return Object.assign({}, p, { newCategory: e.target.value }); }); },
                      style: Object.assign({}, modalInput, { width: "100%" })
                    }),
                    // datalist 提供已有分类的下拉提示
                    h("datalist", { id: "cat-list" },
                      st.existingCategories.map(function (cat) { return h("option", { key: cat, value: cat }); })
                    )
                  ),
                  h("div", null,
                    h("label", { style: { fontSize: "11px", color: "#888", display: "block", marginBottom: "4px" } }, "分组标题"),
                    h(Input, { placeholder: "新分组的标题", value: st.newTitle,
                      onChange: function (e) { setSt(function (p) { return Object.assign({}, p, { newTitle: e.target.value }); }); } })
                  )
                ),
          // 公共字段：条目标题 + 内容
          h("div", null,
            h("label", { style: { fontSize: "11px", color: "#888", display: "block", marginBottom: "4px" } }, "条目标题"),
            h(Input, { placeholder: "条目标题", value: st.header,
              onChange: function (e) { setSt(function (p) { return Object.assign({}, p, { header: e.target.value }); }); } })
          ),
          h("div", null,
            h("label", { style: { fontSize: "11px", color: "#888", display: "block", marginBottom: "4px" } }, "条目内容"),
            h("textarea", { placeholder: "内容（Markdown）", value: st.body,
              onChange: function (e) { setSt(function (p) { return Object.assign({}, p, { body: e.target.value }); }); },
              style: { minHeight: "120px", fontFamily: "monospace", fontSize: "13px", background: "#111", color: "#ccc",
                border: "1px solid #333", borderRadius: "4px", padding: "8px", resize: "vertical" } })
          )
        ),
        st.error && h("div", { style: { color: "#e53e3e", fontSize: "12px", marginTop: "8px" } }, st.error),
        h("div", { style: { display: "flex", justifyContent: "flex-end", gap: "8px", marginTop: "16px" } },
          h(Button, { outlined: true, onClick: props.onClose }, "取消"),
          h(Button, { onClick: create, disabled: st.saving || st.loading }, st.saving ? "创建中…" : "创建")
        )
      )
    );
  }

  // ── 主页面 ────────────────────────────────────────────────────────────

  var TABS = [
    { id: "index", label: "INDEX" },
    { id: "search", label: "搜索" },
    { id: "cold", label: "冷存储" },
    { id: "stats", label: "统计" },
  ];

  function NotesPage() {
    var s = React.useState({ activeTab: "index", selectedNode: null, refreshKey: 0, showNew: false });
    var st = s[0], setSt = s[1];

    var selectEntry = React.useCallback(function (id) {
      setSt(function (p) { return Object.assign({}, p, { selectedNode: { type: "entry", id: id } }); });
    }, []);

    var selectNode = React.useCallback(function (node) {
      setSt(function (p) { return Object.assign({}, p, { selectedNode: node }); });
    }, []);

    var refresh = React.useCallback(function () {
      setSt(function (p) { return Object.assign({}, p, { refreshKey: p.refreshKey + 1 }); });
    }, []);

    // 右侧面板：根据 selectedNode 渲染对应编辑器
    var rightPanel = null;
    var node = st.selectedNode;
    if (node) {
      if (node.type === "entry") {
        rightPanel = h(EntryEditor, {
          entryId: node.id,
          onSaved: refresh,
          onDeleted: function () { setSt(function (p) { return Object.assign({}, p, { selectedNode: null, refreshKey: p.refreshKey + 1 }); }); },
        });
      } else if (node.type === "file") {
        rightPanel = h(FileEditor, {
          key: "file:" + node.id + ":" + st.refreshKey,
          id: node.id,
          title: node.title,
          category: node.category,
          dirty: node.dirty,
          entryCount: node.entryCount || 0,
          onSaved: function () { setSt(function (p) { return Object.assign({}, p, { selectedNode: null, refreshKey: p.refreshKey + 1 }); }); },
          onDeleted: function () { setSt(function (p) { return Object.assign({}, p, { selectedNode: null, refreshKey: p.refreshKey + 1 }); }); },
        });
      } else if (node.type === "category") {
        rightPanel = h(CategoryEditor, {
          key: "cat:" + node.name + ":" + st.refreshKey,
          name: node.name,
          fileCount: node.fileCount || 0,
          onSaved: function () { setSt(function (p) { return Object.assign({}, p, { selectedNode: null, refreshKey: p.refreshKey + 1 }); }); },
          onDeleted: function () { setSt(function (p) { return Object.assign({}, p, { selectedNode: null, refreshKey: p.refreshKey + 1 }); }); },
        });
      }
    }

    return h("div", { style: { display: "flex", flexDirection: "column", height: "100%" } },
      // toolbar
      h("div", { style: { display: "flex", alignItems: "center", gap: "4px", padding: "8px 12px", borderBottom: "1px solid #222" } },
        TABS.map(function (tab) {
          var isActive = st.activeTab === tab.id;
          return h("button", {
            key: tab.id,
            onClick: function () { setSt(function (p) { return Object.assign({}, p, { activeTab: tab.id }); }); },
            style: {
              padding: "6px 14px", fontSize: "12px", fontWeight: 500, cursor: "pointer", borderRadius: "4px", border: "none",
              background: isActive ? "#3b82f6" : "transparent",
              color: isActive ? "##fff" : "#888",
            }
          }, tab.label);
        }),
        h("div", { style: { flex: 1 } }),
        h(Button, { size: "sm", onClick: function () { setSt(function (p) { return Object.assign({}, p, { showNew: true }); }); } }, "+ 新建条目")
      ),
      // body — two columns
      h("div", { style: { display: "flex", flex: 1, overflow: "hidden" } },
        // left panel
        h("div", { style: { width: "280px", borderRight: "1px solid #222", overflow: "hidden", display: "flex", flexDirection: "column" } },
          st.activeTab === "index" && h(IndexTree, { onSelectEntry: selectEntry, onSelectNode: selectNode, selectedNode: st.selectedNode, refreshKey: st.refreshKey }),
          st.activeTab === "search" && h(SearchPanel, { onSelectEntry: selectEntry }),
          st.activeTab === "cold" && h(ColdPanel, { onSelectEntry: selectEntry }),
          st.activeTab === "stats" && h(StatsPanel)
        ),
        // right panel
        st.activeTab !== "stats" && h("div", { style: { flex: 1, overflow: "hidden" } },
          rightPanel || h("div", { style: { padding: "24px", color: "#888", fontSize: "13px" } }, "在左侧选择一个条目、分组或分类进行编辑")
        )
      ),
      // new entry modal
      st.showNew && h(NewEntryModal, {
        onClose: function () { setSt(function (p) { return Object.assign({}, p, { showNew: false }); }); },
        onCreated: function (newId) { setSt(function (p) { return Object.assign({}, p, { showNew: false, refreshKey: p.refreshKey + 1, selectedNode: { type: "entry", id: newId }, activeTab: "index" }); }); },
      })
    );
  }

  // ── Register ──────────────────────────────────────────────────────────

  if (window.__HERMES_PLUGINS__ && typeof window.__HERMES_PLUGINS__.register === "function") {
    window.__HERMES_PLUGINS__.register("sqlite-note-store", NotesPage);
  }
})();
