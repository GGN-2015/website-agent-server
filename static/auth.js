const errorText = document.getElementById("error");

if (errorText && new URLSearchParams(window.location.search).get("error")) {
  errorText.hidden = false;
}
