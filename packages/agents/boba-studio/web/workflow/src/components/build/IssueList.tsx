import type { ReactElement } from "react";

import type { SpecIssue } from "../../model/spec";

type Props = {
  issues: SpecIssue[];
};

/** Замечания валидации пилюлями под панелью билдера. */
export function IssueList({ issues }: Props): ReactElement | null {
  if (issues.length === 0) {
    return null;
  }

  return (
    <div className="issues" aria-label="issues">
      {issues.map((issue, index) => (
        <span className="issues__item" key={`${issue.code}:${issue.where}:${index}`}>
          <span className="issues__code">{issue.code}</span>
          {issue.where !== "" && <span className="issues__where">{issue.where}</span>}
          <span>{issue.message}</span>
        </span>
      ))}
    </div>
  );
}
