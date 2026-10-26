import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { absoluteTime } from "@/lib/relativeTime";
import { ToolValueView } from "./ToolValueView";

afterEach(cleanup);

function renderValue(value: unknown) {
  return render(<ToolValueView value={value} />).container;
}

describe("ToolValueView", () => {
  it("shows nesting past three levels as compact JSON", () => {
    const container = renderValue({ a: { b: { c: { d: 1 } } } });
    expect(container.textContent).toContain('{"d":1}');
  });

  it("caps long field lists and record lists with a count of the rest", () => {
    // WHY: a huge tool payload must not mount one DOM row per entry.
    const fields = Object.fromEntries(
      Array.from({ length: 60 }, (_, index) => [`k${index}`, index]),
    );
    renderValue(fields);
    expect(screen.getByText("and 10 more")).toBeInTheDocument();
    cleanup();

    const records = Array.from({ length: 55 }, (_, index) => ({ title: `Issue ${index}` }));
    renderValue({ issues: records });
    expect(screen.getByText("and 5 more")).toBeInTheDocument();
  });

  it("renders a URL as an external link and an ISO time in local time", () => {
    renderValue({ url: "https://example.com/a", updatedAt: "2026-10-26T09:30:00Z" });
    const link = screen.getByRole("link", { name: "https://example.com/a" });
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    const time = screen.getByText(absoluteTime(Date.parse("2026-10-26T09:30:00Z")));
    expect(time).toHaveAttribute("datetime", "2026-10-26T09:30:00Z");
  });

  it("shows short value lists as chips", () => {
    renderValue({ labels: ["web", "ux"] });
    expect(screen.getByText("web")).toHaveAttribute("data-slot", "badge");
  });

  it("keeps a record's text id and collapses its long extra values to one line", () => {
    const description = "first line\nsecond line";
    const container = renderValue([
      { id: "OMNI-12243", title: "Tool cards", notes: description, status: "In Review" },
    ]);
    expect(container.textContent).toContain("ID OMNI-12243");
    expect(container.textContent).toContain("Notes first line second line");
    expect(container.querySelector("pre")).toBeNull();
  });

  it("labels inherited object names as plain words", () => {
    renderValue({ constructor: "x" });
    expect(screen.getByText("Constructor")).toBeInTheDocument();
  });
});
