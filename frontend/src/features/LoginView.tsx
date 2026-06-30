import { FormEvent, useState } from "react";

interface LoginViewProps {
  login: (username: string, password: string) => Promise<void>;
  error: string;
}

export function LoginView({ login, error }: LoginViewProps) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("admin");
  const [submitting, setSubmitting] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    try {
      await login(username, password);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="login-shell">
      <section className="login-panel">
        <div className="login-brand">
          <div className="brand-mark">L</div>
          <div>
            <span className="brand-kicker">LGDO Console</span>
            <h1>知识工作台登录</h1>
            <p>使用本地账户进入内部知识库，角色和 ACL 标签会控制资料、问答与管理入口。</p>
          </div>
        </div>

        <form className="login-form" onSubmit={submit}>
          <label>
            账户
            <input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" />
          </label>
          <label>
            密码
            <input
              value={password}
              type="password"
              onChange={(event) => setPassword(event.target.value)}
              autoComplete="current-password"
            />
          </label>
          {error && <div className="form-error">{error}</div>}
          <button className="primary-action full-button" type="submit" disabled={submitting}>
            {submitting ? "登录中" : "登录"}
          </button>
        </form>

        <div className="login-footnote">
          <span className="tiny-dot ok"></span>
          <span>默认本地管理员：admin / admin，可在账户权限中修改。</span>
        </div>
      </section>
    </main>
  );
}
