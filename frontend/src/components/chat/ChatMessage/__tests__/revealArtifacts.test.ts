import test from "node:test";
import assert from "node:assert/strict";

import {
  buildRevealArtifactTree,
  collectRevealArtifacts,
  getRevealArtifactStats,
  type RevealArtifact,
} from "../revealArtifacts.ts";

test("collects successful file and project reveal artifacts from current message parts", () => {
  const artifacts = collectRevealArtifacts([
    {
      type: "tool",
      name: "reveal_file",
      args: {},
      success: true,
      result: {
        key: "revealed/report.pdf",
        url: "/api/upload/file/revealed/report.pdf",
        name: "report.pdf",
        type: "document",
        mime_type: "application/pdf",
        size: 2048,
        _meta: {
          path: "/workspace/report.pdf",
          description: "Final report",
        },
      },
    },
    {
      type: "tool",
      name: "reveal_file",
      args: { path: "/workspace/draft.md" },
      success: false,
      result: {
        key: "revealed/draft.md",
        url: "/api/upload/file/revealed/draft.md",
        name: "draft.md",
        type: "document",
        size: 10,
      },
    },
    {
      type: "subagent",
      agent_id: "agent-1",
      agent_name: "worker",
      input: "build project",
      depth: 1,
      parts: [
        {
          type: "tool",
          name: "reveal_project",
          args: { project_path: "/workspace/site", name: "site" },
          success: true,
          result: {
            type: "project_reveal",
            version: 2,
            name: "site",
            mode: "project",
            template: "react",
            path: "/workspace/site",
            files: {
              "/src/main.tsx": {
                url: "/api/upload/file/revealed/main",
                is_binary: false,
                size: 100,
              },
            },
          },
        },
      ],
    },
  ]);

  assert.equal(artifacts.length, 2);
  assert.deepEqual(
    artifacts.map((artifact) => ({
      kind: artifact.kind,
      name: artifact.name,
      previewKey: artifact.preview.previewKey,
    })),
    [
      {
        kind: "file",
        name: "report.pdf",
        previewKey: "revealed/report.pdf",
      },
      {
        kind: "project",
        name: "site",
        previewKey: "/workspace/site",
      },
    ],
  );
});

test("builds stable nested artifact tree metadata", () => {
  const artifacts: RevealArtifact[] = [
    {
      kind: "file",
      id: "file:src/app/page.tsx",
      name: "page.tsx",
      path: "/workspace/site/src/app/page.tsx",
      preview: {
        kind: "file",
        previewKey: "src/app/page.tsx",
        filePath: "/workspace/site/src/app/page.tsx",
      },
    },
    {
      kind: "file",
      id: "file:src/app/styles.css",
      name: "styles.css",
      path: "/workspace/site/src/app/styles.css",
      preview: {
        kind: "file",
        previewKey: "src/app/styles.css",
        filePath: "/workspace/site/src/app/styles.css",
      },
    },
  ];

  const tree = buildRevealArtifactTree(
    artifacts.filter(
      (a): a is RevealArtifact & { kind: "file" } => a.kind === "file",
    ),
  );
  const workspace = tree.children[0];
  assert.equal(workspace.kind, "dir");
  if (workspace.kind !== "dir") return;

  assert.equal(workspace.path, "workspace");
  assert.equal(workspace.fileCount, 2);
  assert.equal(workspace.dirCount, 3);

  const stats = getRevealArtifactStats(artifacts);
  assert.deepEqual(stats, {
    fileCount: 2,
    projectCount: 0,
    totalCount: 2,
  });
});
