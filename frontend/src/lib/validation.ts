const ALLOWED_HOSTS = ["readawrite.com", "www.readawrite.com"];

/** Basic client-side check. The backend validation is the authority. */
export function validateUrl(value: string): string | null {
  const text = value.trim();
  if (!text) return "Please paste a ReadAWrite URL.";

  let url: URL;
  try {
    url = new URL(text);
  } catch {
    return "That doesn't look like a valid URL. Include https://";
  }

  if (url.protocol !== "https:") return "The URL must start with https://";
  if (!ALLOWED_HOSTS.includes(url.hostname.toLowerCase())) {
    return "Only readawrite.com URLs are supported.";
  }
  return null;
}

export function validateOutputName(value: string): string | null {
  const text = value.trim();
  if (!text) return null; // optional
  if (text.length > 100) return "Use 100 characters or fewer.";
  if (/[\\/]/.test(text)) return "Folder names can't contain / or \\.";
  return null;
}
