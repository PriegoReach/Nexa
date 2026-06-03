import { useState } from "react";
import { Login } from "./components/Login";
import { Workspace } from "./components/Workspace";

// El JWT vive SOLO en el estado de React (no localStorage): se pierde al recargar,
// lo cual es lo deseado para el MVP y evita el riesgo de tokens persistidos en
// almacenamiento accesible por scripts. Sin token -> Login. Con token -> Chat.
export default function App() {
  const [token, setToken] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  function handleSignOut(message?: string) {
    setToken(null);
    setNotice(message ?? null);
  }

  if (!token) {
    return (
      <Login
        notice={notice}
        onAuthenticated={(t) => {
          setToken(t);
          setNotice(null);
        }}
      />
    );
  }

  return (
    <Workspace
      token={token}
      onSignOut={() => handleSignOut()}
      onSessionExpired={() =>
        handleSignOut("Tu sesión expiró. Inicia sesión de nuevo.")
      }
    />
  );
}
