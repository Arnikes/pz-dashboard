"use strict";

const form = document.getElementById("loginForm");
const button = document.getElementById("loginSubmit");
const error = document.getElementById("loginError");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  button.disabled = true;
  button.textContent = "Вход…";
  error.hidden = true;
  try {
    const response = await fetch("/api/auth/login", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-PZ-Request": "1" },
      body: JSON.stringify({
        login: document.getElementById("login").value,
        password: document.getElementById("password").value,
      }),
      signal: AbortSignal.timeout(10000),
    });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || "Не удалось войти");
    const next = new URLSearchParams(location.search).get("next") || location.hash;
    location.replace("/" + (/^#\/[a-z-]+$/.test(next) ? next : "#/overview"));
  } catch (failure) {
    error.textContent = failure instanceof TypeError || failure.name === "TimeoutError"
      ? "Нет связи с пультом. Повторите попытку."
      : failure.message;
    error.hidden = false;
    document.getElementById("password").value = "";
    document.getElementById("password").focus();
  } finally {
    button.disabled = false;
    button.textContent = "Войти";
  }
});
