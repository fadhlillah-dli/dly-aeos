import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AddUrlForm } from "./index";

describe("research registration and file upload (§01 U-11)", () => {
  it("says honestly that files are not uploaded here and where they go instead", () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <AddUrlForm />
      </QueryClientProvider>,
    );
    const note = screen.getByTestId("research-upload-note");
    expect(note).toHaveTextContent("tidak diunggah di sini");
    expect(note).toHaveTextContent("aeos intake upload");
    expect(screen.queryByLabelText(/berkas|unggah/i)).toBeNull(); // no fake upload control
  });
});
