import { useMemo, useState } from "react";

import type { AccountRecord, AuthUser } from "../types";
import { splitTags } from "../utils/format";

interface AccountsTaskProps {
  accounts: AccountRecord[];
  currentUser: AuthUser | null;
  createAccount: (payload: AccountPayload) => Promise<void>;
  updateAccount: (userId: string, payload: Partial<AccountPayload>) => Promise<void>;
  showToast: (message: string) => void;
}

export interface AccountPayload {
  user_id: string;
  username: string;
  password?: string;
  role: "admin" | "editor" | "viewer";
  acl_tags: string[];
  status: "active" | "disabled";
}

export function AccountsTask({ accounts, currentUser, createAccount, updateAccount, showToast }: AccountsTaskProps) {
  const [selectedId, setSelectedId] = useState("");
  const selected = useMemo(
    () => accounts.find((account) => account.user_id === selectedId) || accounts[0],
    [accounts, selectedId],
  );
  const [draft, setDraft] = useState({
    user_id: "",
    username: "",
    password: "",
    role: "viewer" as AccountPayload["role"],
    acl_tags: "internal",
    status: "active" as AccountPayload["status"],
  });
  const [editPassword, setEditPassword] = useState("");

  const activeAccounts = accounts.filter((account) => account.status === "active").length;
  const adminAccounts = accounts.filter((account) => account.role === "admin").length;
  const managedTags = Array.from(new Set(accounts.flatMap((account) => account.acl_tags))).filter(Boolean);

  async function submitCreate() {
    if (!draft.user_id.trim()) return showToast("请填写账户 ID");
    await createAccount({
      user_id: draft.user_id.trim(),
      username: draft.username.trim() || draft.user_id.trim(),
      password: draft.password || undefined,
      role: draft.role,
      acl_tags: splitTags(draft.acl_tags),
      status: draft.status,
    });
    setDraft({ user_id: "", username: "", password: "", role: "viewer", acl_tags: "internal", status: "active" });
  }

  async function saveAccount(userId: string, payload: Partial<AccountPayload>) {
    await updateAccount(userId, payload);
    setEditPassword("");
  }

  return (
    <div className="accounts-workspace">
      <section className="panel">
        <div className="panel-title">
          <h2>账户权限总览</h2>
          <span className="pill strong-pill">{currentUser?.role || "viewer"}</span>
        </div>
        <div className="account-summary">
          <Summary label="账户" value={accounts.length} />
          <Summary label="启用" value={activeAccounts} />
          <Summary label="管理员" value={adminAccounts} />
          <Summary label="ACL 标签" value={managedTags.length} />
        </div>
      </section>

      <section className="accounts-grid">
        <div className="panel">
          <div className="panel-title">
            <h2>账户列表</h2>
            <span className="pill">{accounts.length} 个账户</span>
          </div>
          <div className="list account-list">
            {accounts.map((account) => (
              <button
                className={`account-row ${selected?.user_id === account.user_id ? "active" : ""}`}
                key={account.user_id}
                type="button"
                onClick={() => {
                  setSelectedId(account.user_id);
                  setEditPassword("");
                }}
              >
                <span className="account-avatar">{account.username?.slice(0, 1) || account.user_id.slice(0, 1)}</span>
                <span>
                  <strong>{account.username || account.user_id}</strong>
                  <small>{account.user_id} · {account.role}</small>
                </span>
                <span className={`pill ${account.status === "active" ? "strong-pill" : "muted-pill"}`}>
                  {account.status === "active" ? "启用" : "禁用"}
                </span>
              </button>
            ))}
          </div>
        </div>

        <div className="panel">
          <div className="panel-title">
            <h2>编辑账户 ACL</h2>
            <span className="pill">{selected?.user_id || "未选择"}</span>
          </div>
          {selected ? (
            <div key={selected.user_id}>
              <AccountEditor
                account={selected}
                password={editPassword}
                setPassword={setEditPassword}
                updateAccount={saveAccount}
              />
            </div>
          ) : (
            <p>暂无账户。</p>
          )}
        </div>

        <div className="panel">
          <div className="panel-title">
            <h2>新增账户</h2>
            <span className="pill">本地登录</span>
          </div>
          <div className="grid-2">
            <label>
              账户 ID
              <input value={draft.user_id} onChange={(event) => setDraft({ ...draft, user_id: event.target.value })} />
            </label>
            <label>
              显示名
              <input value={draft.username} onChange={(event) => setDraft({ ...draft, username: event.target.value })} />
            </label>
          </div>
          <div className="grid-3">
            <label>
              角色
              <select value={draft.role} onChange={(event) => setDraft({ ...draft, role: event.target.value as AccountPayload["role"] })}>
                <option value="viewer">viewer</option>
                <option value="editor">editor</option>
                <option value="admin">admin</option>
              </select>
            </label>
            <label>
              状态
              <select value={draft.status} onChange={(event) => setDraft({ ...draft, status: event.target.value as AccountPayload["status"] })}>
                <option value="active">启用</option>
                <option value="disabled">禁用</option>
              </select>
            </label>
            <label>
              初始密码
              <input value={draft.password} type="password" onChange={(event) => setDraft({ ...draft, password: event.target.value })} />
            </label>
          </div>
          <label>
            ACL 标签
            <input value={draft.acl_tags} onChange={(event) => setDraft({ ...draft, acl_tags: event.target.value })} />
          </label>
          <button className="primary-action" type="button" onClick={() => submitCreate().catch((error) => showToast(error.message))}>
            创建账户
          </button>
        </div>
      </section>
    </div>
  );
}

function AccountEditor({
  account,
  password,
  setPassword,
  updateAccount,
}: {
  account: AccountRecord;
  password: string;
  setPassword: (value: string) => void;
  updateAccount: (userId: string, payload: Partial<AccountPayload>) => Promise<void>;
}) {
  const [username, setUsername] = useState(account.username || account.user_id);
  const [role, setRole] = useState(account.role as AccountPayload["role"]);
  const [status, setStatus] = useState(account.status as AccountPayload["status"]);
  const [aclTags, setAclTags] = useState(account.acl_tags.join(", "));

  return (
    <div className="account-editor">
      <div className="grid-2">
        <label>
          显示名
          <input value={username} onChange={(event) => setUsername(event.target.value)} />
        </label>
        <label>
          新密码
          <input value={password} type="password" onChange={(event) => setPassword(event.target.value)} placeholder="留空则不修改" />
        </label>
      </div>
      <div className="grid-2">
        <label>
          角色
          <select value={role} onChange={(event) => setRole(event.target.value as AccountPayload["role"])}>
            <option value="viewer">viewer</option>
            <option value="editor">editor</option>
            <option value="admin">admin</option>
          </select>
        </label>
        <label>
          状态
          <select value={status} onChange={(event) => setStatus(event.target.value as AccountPayload["status"])}>
            <option value="active">启用</option>
            <option value="disabled">禁用</option>
          </select>
        </label>
      </div>
      <label>
        ACL 标签
        <input value={aclTags} onChange={(event) => setAclTags(event.target.value)} />
      </label>
      <div className="meta account-tags">
        {account.acl_tags.map((tag) => <span className="pill" key={tag}>{tag}</span>)}
      </div>
      <button
        className="primary-action"
        type="button"
        onClick={() =>
          updateAccount(account.user_id, {
            username,
            role,
            status,
            acl_tags: splitTags(aclTags),
            password: password || undefined,
          })
        }
      >
        保存权限
      </button>
    </div>
  );
}

function Summary({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <strong>{value}</strong>
      <span>{label}</span>
    </div>
  );
}
