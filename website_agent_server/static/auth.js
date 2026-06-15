const errorText = document.getElementById("error");
const nextUrlInput = document.getElementById("next-url");

const params = new URLSearchParams(window.location.search);

if (nextUrlInput) {
  const nextUrl = params.get("next_url");
  const safeNextUrl = nextUrl && nextUrl.startsWith("/") && !nextUrl.startsWith("//") ? nextUrl : "/";
  nextUrlInput.value = `${safeNextUrl}${window.location.hash || ""}`;
}

if (errorText && params.get("error")) {
  errorText.hidden = false;
}
