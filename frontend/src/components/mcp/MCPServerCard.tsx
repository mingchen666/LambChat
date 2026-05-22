import { useState, useCallback, useRef, useMemo } from "react";
import {
  Server,
  ToggleLeft,
  ToggleRight,
  Edit3,
  Trash2,
  ChevronDown,
  ChevronRight,
  Wrench,
  Loader2,
  Shield,
} from "lucide-react";
import { useTranslation } from "react-i18next";
import toast from "react-hot-toast";
import { mcpApi } from "../../services/api/mcp";
import type { MCPRoleQuota, MCPServerResponse, MCPToolInfo } from "../../types";
import { formatDate } from "../../utils/datetime";
import { RoleSelector } from "./RoleSelector";

interface MCPServerCardProps {
  server: MCPServerResponse;
  onToggle: (name: string) => void;
  onEdit: (server: MCPServerResponse) => void;
  onDelete: (name: string, isSystem: boolean) => void;
  onToolToggled?: () => void;
}

const TRANSPORT_COLORS: Record<string, string> = {
  sse: "bg-emerald-50 text-emerald-600 ring-1 ring-emerald-200 dark:bg-emerald-950/40 dark:text-emerald-400 dark:ring-emerald-800/60",
  streamable_http:
    "bg-violet-50 text-violet-600 ring-1 ring-violet-200 dark:bg-violet-950/40 dark:text-violet-400 dark:ring-violet-800/60",
  sandbox:
    "bg-amber-50 text-amber-600 ring-1 ring-amber-200 dark:bg-amber-950/40 dark:text-amber-400 dark:ring-amber-800/60",
};

const DEFAULT_TRANSPORT_COLOR =
  "bg-stone-50 text-stone-600 ring-1 ring-stone-200 dark:bg-stone-800 dark:text-stone-400 dark:ring-stone-700";

function trimQuotasForRoles(
  roles: string[],
  quotas: Record<string, MCPRoleQuota> = {},
): Record<string, MCPRoleQuota> {
  return Object.fromEntries(
    roles.filter((role) => quotas[role]).map((role) => [role, quotas[role]]),
  );
}

function updateQuotaValue(
  quotas: Record<string, MCPRoleQuota> = {},
  role: string,
  field: keyof MCPRoleQuota,
  value: string,
): Record<string, MCPRoleQuota> | null {
  if (value !== "") {
    const num = Number(value);
    if (!Number.isInteger(num) || num < 0) return null;
  }

  const next = { ...quotas };
  const current = next[role] ?? {};
  const updated: MCPRoleQuota = {
    ...current,
    [field]: value === "" ? null : Number(value),
  };
  if (updated.daily_limit == null && updated.weekly_limit == null) {
    delete next[role];
  } else {
    next[role] = updated;
  }
  return next;
}

export function MCPServerCard({
  server,
  onToggle,
  onEdit,
  onDelete,
  onToolToggled,
}: MCPServerCardProps) {
  const { t } = useTranslation();
  const [isToolsExpanded, setIsToolsExpanded] = useState(false);
  const [tools, setTools] = useState<MCPToolInfo[]>([]);
  const [toolsLoading, setToolsLoading] = useState(false);
  const [toolsError, setToolsError] = useState<string | null>(null);
  const [savingToolPolicy, setSavingToolPolicy] = useState<string | null>(null);

  // Track pending toggle to debounce rapid clicks and avoid race conditions
  const pendingToggleRef = useRef<Promise<void> | null>(null);

  const TRANSPORT_LABELS: Record<string, string> = {
    sse: t("mcp.form.transportSse"),
    streamable_http: t("mcp.form.transportHttp"),
    sandbox: t("mcp.form.transportSandbox"),
  };
  const transportLabel =
    TRANSPORT_LABELS[server.transport] || server.transport.toUpperCase();
  const transportColor =
    TRANSPORT_COLORS[server.transport] || DEFAULT_TRANSPORT_COLOR;

  const handleToggleTools = useCallback(async () => {
    if (isToolsExpanded) {
      setIsToolsExpanded(false);
      return;
    }

    // If we haven't loaded tools yet, fetch them
    if (tools.length === 0 && !toolsLoading) {
      setIsToolsExpanded(true);
      setToolsLoading(true);
      setToolsError(null);
      try {
        const result = await mcpApi.discoverTools(server.name);
        if (result.error) {
          setToolsError(result.error);
        } else {
          // Sort tools by name
          const sortedTools = [...result.tools].sort((a, b) =>
            a.name.toLowerCase().localeCompare(b.name.toLowerCase()),
          );
          setTools(sortedTools);
        }
      } catch (err) {
        setToolsError(
          err instanceof Error ? err.message : t("mcp.card.discoverFailed"),
        );
      } finally {
        setToolsLoading(false);
      }
    } else {
      setIsToolsExpanded(true);
    }
  }, [isToolsExpanded, tools.length, toolsLoading, server.name, t]);

  const handleToggleTool = useCallback(
    async (toolName: string, currentEnabled: boolean) => {
      const newEnabled = !currentEnabled;

      // Serialize toggles: wait for any in-flight toggle, then run this one
      const togglePromise = (async () => {
        if (pendingToggleRef.current) {
          await pendingToggleRef.current;
        }

        try {
          // user level: per-user preference toggle
          if (server.can_edit) {
            await mcpApi.toggleSystemTool(server.name, toolName, newEnabled);
          } else {
            await mcpApi.toggleTool(server.name, toolName, newEnabled, "user");
          }
          setTools((prev) =>
            prev.map((t) =>
              t.name === toolName
                ? server.can_edit
                  ? { ...t, system_disabled: !newEnabled }
                  : { ...t, user_disabled: !newEnabled }
                : t,
            ),
          );
          onToolToggled?.();
        } catch {
          toast.error(t("mcp.card.toolToggleFailed", "Failed to toggle tool"));
          onToolToggled?.();
        }
      })();

      pendingToggleRef.current = togglePromise;
      await togglePromise;
      pendingToggleRef.current = null;
    },
    [server.can_edit, server.name, onToolToggled, t],
  );

  const handleUpdateToolPolicy = useCallback(
    async (
      toolName: string,
      updates: {
        allowed_roles?: string[];
        role_quotas?: Record<string, MCPRoleQuota>;
        disabled?: boolean;
      },
    ) => {
      const current = tools.find((tool) => tool.name === toolName);
      if (!current) return;

      const nextAllowedRoles =
        updates.allowed_roles ?? current.allowed_roles ?? [];
      const nextRoleQuotas = updates.role_quotas ?? current.role_quotas ?? {};
      const nextDisabled = updates.disabled ?? current.system_disabled ?? false;

      setSavingToolPolicy(toolName);
      try {
        await mcpApi.updateToolPolicy(server.name, toolName, {
          disabled: nextDisabled,
          allowed_roles: nextAllowedRoles,
          role_quotas: nextRoleQuotas,
        });
        setTools((prev) =>
          prev.map((tool) =>
            tool.name === toolName
              ? {
                  ...tool,
                  system_disabled: nextDisabled,
                  allowed_roles: nextAllowedRoles,
                  role_quotas: nextRoleQuotas,
                  policy_configured: true,
                }
              : tool,
          ),
        );
        onToolToggled?.();
      } catch {
        toast.error(
          t("mcp.card.toolPolicyUpdateFailed", "Failed to update tool policy"),
        );
      } finally {
        setSavingToolPolicy(null);
      }
    },
    [onToolToggled, server.name, t, tools],
  );

  // Count visible (non-disabled) tools
  const enabledToolCount = useMemo(
    () =>
      tools.length > 0
        ? tools.filter((t) => !t.system_disabled && !t.user_disabled).length
        : 0,
    [tools],
  );

  return (
    <div
      className={`panel-card group/card transition-all duration-200 ${
        !server.enabled ? "opacity-50 saturate-50" : "hover:shadow-md"
      }`}
    >
      <div className="flex items-start justify-between">
        <div className="flex-1 min-w-0">
          <div className="flex flex-wrap items-center gap-1.5">
            <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-stone-100 dark:bg-stone-800">
              <Server
                size={14}
                className="text-stone-500 dark:text-stone-400"
              />
            </div>
            <h4 className="font-semibold text-[var(--theme-text)] text-sm truncate">
              {server.name}
            </h4>
            <span
              className={`rounded-full px-2 py-0.5 text-[11px] font-medium tracking-wide ${transportColor}`}
            >
              {transportLabel}
            </span>
            <span
              className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${
                server.is_internal
                  ? "bg-sky-50 text-sky-600 ring-1 ring-sky-200 dark:bg-sky-950/40 dark:text-sky-400 dark:ring-sky-800/60"
                  : server.is_system
                    ? "bg-orange-50 text-orange-600 ring-1 ring-orange-200 dark:bg-orange-950/40 dark:text-orange-400 dark:ring-orange-800/60"
                    : "bg-stone-50 text-stone-600 ring-1 ring-stone-200 dark:bg-stone-800 dark:text-stone-400 dark:ring-stone-700"
              }`}
            >
              {server.is_internal
                ? t("mcp.card.internal", "Internal")
                : server.is_system
                  ? t("mcp.card.system")
                  : t("mcp.card.user")}
            </span>
            {server.is_system &&
              server.allowed_roles &&
              server.allowed_roles.length > 0 && (
                <span
                  className="inline-flex items-center gap-1 rounded-full bg-blue-50 px-2 py-0.5 text-[11px] font-medium text-blue-600 ring-1 ring-blue-200 dark:bg-blue-950/40 dark:text-blue-400 dark:ring-blue-800/60"
                  title={t("mcp.card.allowedRolesTooltip", {
                    roles: server.allowed_roles.join(", "),
                  })}
                >
                  <Shield size={10} />
                  {server.allowed_roles.length === 1
                    ? server.allowed_roles[0]
                    : t("mcp.card.roleCount", {
                        count: server.allowed_roles.length,
                      })}
                </span>
              )}
            {!server.enabled && (
              <span className="rounded-full bg-red-50 px-2 py-0.5 text-[11px] font-medium text-red-500 ring-1 ring-red-200 dark:bg-red-950/30 dark:text-red-400 dark:ring-red-800/40">
                {t("mcp.card.disabled")}
              </span>
            )}
          </div>

          {/* Transport-specific details */}
          <div className="mt-2.5 text-sm text-stone-600 dark:text-stone-400 space-y-1">
            {server.url && (
              <div className="inline-flex items-start gap-1.5 font-mono text-xs bg-stone-50 dark:bg-stone-800/80 rounded-md px-2 py-1 break-all max-w-full">
                <span className="text-stone-400 dark:text-stone-500 select-none flex-shrink-0">
                  →
                </span>
                <span>{server.url}</span>
              </div>
            )}
            {server.command && (
              <div className="inline-flex items-start gap-1.5 font-mono text-xs bg-stone-50 dark:bg-stone-800/80 rounded-md px-2 py-1 break-all max-w-full">
                <span className="text-stone-400 dark:text-stone-500 select-none flex-shrink-0">
                  $
                </span>
                <span>{server.command}</span>
              </div>
            )}
          </div>

          {/* Headers info */}
          {server.headers && Object.keys(server.headers).length > 0 && (
            <div className="mt-1.5 inline-flex items-center gap-1 text-[11px] text-stone-400 dark:text-stone-500">
              <span className="h-1 w-1 rounded-full bg-stone-300 dark:bg-stone-600" />
              {t("mcp.card.headersCount", {
                count: Object.keys(server.headers).length,
              })}
            </div>
          )}

          {/* Env keys info (sandbox transport) */}
          {server.env_keys && server.env_keys.length > 0 && (
            <div className="mt-1.5 inline-flex items-center gap-1 text-[11px] text-stone-400 dark:text-stone-500">
              <span className="h-1 w-1 rounded-full bg-stone-300 dark:bg-stone-600" />
              {t("mcp.card.envVarsCount", {
                count: server.env_keys.length,
              })}
            </div>
          )}

          {/* Timestamps */}
          {server.updated_at && (
            <div className="mt-2 text-[11px] text-stone-400/70 dark:text-stone-500/70">
              {t("mcp.card.updated", {
                date: formatDate(server.updated_at),
              })}
            </div>
          )}
        </div>

        {/* Action buttons */}
        <div className="flex items-center gap-0.5 flex-shrink-0 ml-2 opacity-0 transition-opacity duration-150 group-hover/card:opacity-100">
          {!server.is_internal && (
            <button
              onClick={() => onToggle(server.name)}
              className="btn-icon rounded-lg"
              title={
                server.enabled ? t("mcp.card.disable") : t("mcp.card.enable")
              }
            >
              {server.enabled ? (
                <ToggleRight
                  size={20}
                  className="text-emerald-500 dark:text-emerald-400"
                />
              ) : (
                <ToggleLeft size={20} />
              )}
            </button>
          )}
          {server.can_edit && !server.is_internal && (
            <>
              <button
                onClick={() => onEdit(server)}
                className="btn-icon rounded-lg"
                title={t("mcp.card.edit")}
              >
                <Edit3 size={16} />
              </button>
              <button
                onClick={() => onDelete(server.name, server.is_system)}
                className="btn-icon rounded-lg hover:bg-red-50 hover:text-red-500 dark:hover:bg-red-950/30 dark:hover:text-red-400"
                title={t("mcp.card.delete")}
              >
                <Trash2 size={16} />
              </button>
            </>
          )}
        </div>
      </div>

      {/* Tools section */}
      {server.enabled &&
        (server.transport !== "sandbox" || server.is_internal) && (
          <div className="mt-3 border-t border-stone-100 dark:border-stone-700/40 pt-2.5">
            <button
              onClick={handleToggleTools}
              className="group/tools flex items-center gap-1.5 text-xs text-stone-400 dark:text-stone-500 hover:text-stone-600 dark:hover:text-stone-300 transition-colors w-full rounded-md px-1 py-0.5 hover:bg-stone-50 dark:hover:bg-stone-800/50"
            >
              {isToolsExpanded ? (
                <ChevronDown size={13} className="transition-transform" />
              ) : (
                <ChevronRight size={13} className="transition-transform" />
              )}
              <Wrench size={11} />
              <span>{t("mcp.card.tools")}</span>
              {toolsLoading && <Loader2 size={11} className="animate-spin" />}
              {tools.length > 0 && !toolsLoading && (
                <span className="tabular-nums text-stone-300 dark:text-stone-600 group-hover/tools:text-stone-400 dark:group-hover/tools:text-stone-500 transition-colors">
                  {enabledToolCount}/{tools.length}
                </span>
              )}
            </button>

            {isToolsExpanded && (
              <div className="mt-1.5 ml-1 space-y-0.5">
                {toolsLoading && (
                  <div className="flex items-center gap-2 py-2.5 pl-3 text-xs text-stone-400 dark:text-stone-500">
                    <Loader2 size={13} className="animate-spin" />
                    <span>{t("mcp.card.discovering")}</span>
                  </div>
                )}

                {toolsError && (
                  <div className="text-xs text-red-500 dark:text-red-400 py-1 pl-3">
                    {toolsError}
                  </div>
                )}

                {!toolsLoading && tools.length === 0 && !toolsError && (
                  <div className="text-xs text-stone-400 dark:text-stone-500 py-1 pl-3">
                    {t("mcp.card.noTools")}
                  </div>
                )}

                {!toolsLoading &&
                  tools.map((tool) => {
                    const isDisabled =
                      tool.system_disabled || tool.user_disabled || false;
                    return (
                      <div
                        key={tool.name}
                        className={`flex items-start gap-2 py-2 px-2.5 rounded-lg transition-colors ${
                          isDisabled
                            ? "opacity-40"
                            : "hover:bg-stone-50 dark:hover:bg-stone-800/40"
                        }`}
                      >
                        <button
                          onClick={() =>
                            handleToggleTool(tool.name, !isDisabled)
                          }
                          className="mt-0.5 flex-shrink-0 transition-transform active:scale-90"
                          title={
                            isDisabled
                              ? t("mcp.card.enableTool")
                              : t("mcp.card.disableTool")
                          }
                        >
                          {isDisabled ? (
                            <ToggleLeft
                              size={16}
                              className="text-stone-300 dark:text-stone-600"
                            />
                          ) : (
                            <ToggleRight
                              size={16}
                              className="text-emerald-500 dark:text-emerald-400"
                            />
                          )}
                        </button>
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-1.5 flex-wrap">
                            <code className="text-xs font-medium text-stone-700 dark:text-stone-200 truncate">
                              {tool.name}
                            </code>
                            {tool.parameters.length > 0 && (
                              <span className="text-[10px] px-1.5 py-px rounded-full bg-stone-100 dark:bg-stone-700/60 text-stone-400 dark:text-stone-500 tabular-nums">
                                {tool.parameters.length}
                              </span>
                            )}
                          </div>
                          {tool.description && (
                            <p className="text-[11px] text-stone-400 dark:text-stone-500 truncate mt-0.5 leading-relaxed">
                              {tool.description}
                            </p>
                          )}
                          {server.can_edit && (
                            <div className="mt-2.5 rounded-lg border border-stone-100 dark:border-stone-700/50 bg-stone-50/50 dark:bg-stone-800/30 p-2.5">
                              <div className="flex items-center justify-between gap-2 mb-2">
                                <span className="text-[10px] font-semibold uppercase tracking-widest text-stone-400 dark:text-stone-500">
                                  {t("mcp.form.allowedRoles")}
                                </span>
                                {savingToolPolicy === tool.name && (
                                  <Loader2
                                    size={11}
                                    className="animate-spin text-stone-400"
                                  />
                                )}
                              </div>
                              <RoleSelector
                                selectedRoles={tool.allowed_roles ?? []}
                                onChange={(roles) =>
                                  handleUpdateToolPolicy(tool.name, {
                                    allowed_roles: roles,
                                    role_quotas: trimQuotasForRoles(
                                      roles,
                                      tool.role_quotas,
                                    ),
                                  })
                                }
                              />
                              {(tool.allowed_roles ?? []).length > 0 && (
                                <div className="mt-2 space-y-1.5">
                                  {(tool.allowed_roles ?? []).map((role) => {
                                    const quota =
                                      tool.role_quotas?.[role] ?? {};
                                    return (
                                      <div
                                        key={role}
                                        className="grid grid-cols-[1fr_76px_76px] items-center gap-2"
                                      >
                                        <span className="truncate text-[11px] font-medium text-stone-500 dark:text-stone-400">
                                          {role}
                                        </span>
                                        <input
                                          type="number"
                                          min="0"
                                          value={quota.daily_limit ?? ""}
                                          onChange={(e) => {
                                            const nextQuotas = updateQuotaValue(
                                              tool.role_quotas,
                                              role,
                                              "daily_limit",
                                              e.target.value,
                                            );
                                            if (nextQuotas) {
                                              handleUpdateToolPolicy(
                                                tool.name,
                                                {
                                                  role_quotas: nextQuotas,
                                                },
                                              );
                                            }
                                          }}
                                          placeholder={t("mcp.form.dailyLimit")}
                                          className="glass-input h-7 px-2 text-[11px] tabular-nums"
                                        />
                                        <input
                                          type="number"
                                          min="0"
                                          value={quota.weekly_limit ?? ""}
                                          onChange={(e) => {
                                            const nextQuotas = updateQuotaValue(
                                              tool.role_quotas,
                                              role,
                                              "weekly_limit",
                                              e.target.value,
                                            );
                                            if (nextQuotas) {
                                              handleUpdateToolPolicy(
                                                tool.name,
                                                {
                                                  role_quotas: nextQuotas,
                                                },
                                              );
                                            }
                                          }}
                                          placeholder={t(
                                            "mcp.form.weeklyLimit",
                                          )}
                                          className="glass-input h-7 px-2 text-[11px] tabular-nums"
                                        />
                                      </div>
                                    );
                                  })}
                                </div>
                              )}
                            </div>
                          )}
                        </div>
                      </div>
                    );
                  })}
              </div>
            )}
          </div>
        )}
    </div>
  );
}
