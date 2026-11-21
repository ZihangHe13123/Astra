"""The reasoning efforts a person can choose: /mode for the windows, and a channel's own setting."""

# xhigh sits between high and max: deeper reasoning than high at far less than max's cost
# (claude.ai labels max "5.5x or more usage"; Claude Code's docs call max prone to overthinking).
REASONING_EFFORTS = ("low", "high", "xhigh", "max")
DEFAULT_REASONING_EFFORT = "high"
