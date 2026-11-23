const page = document.querySelector("main[data-doc]");
if (page && typeof document.modelContext?.registerTool === "function") {
  document.modelContext.registerTool({
    name: "get_current_wiki_section",
    description: "Read the current wiki revision, selected section and highlighted text.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    annotations: { readOnlyHint: true },
    execute: async () => {
      const selection = window.getSelection();
      const node = selection?.anchorNode;
      const element = node?.nodeType === Node.ELEMENT_NODE ? node : node?.parentElement;
      const section = element?.closest("article[id]") ||
        document.getElementById(location.hash.slice(1)) || page.querySelector("article");
      const end = selection?.focusNode;
      const selectedText = section?.contains(node) && section.contains(end)
        ? selection.toString().slice(0, 3000) : "";
      return {
        doc: page.dataset.doc,
        revision: Number(page.dataset.revision),
        section: section ? Number(section.id.slice(1)) : null,
        selected_text: selectedText,
        url: new URL(section ? "#" + section.id : "", page.dataset.revisionUrl
          ? new URL(page.dataset.revisionUrl, location.href) : location.href).href,
      };
    },
  });
}
