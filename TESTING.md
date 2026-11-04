# End-to-End Test Targets

Use only these QQ targets for live bot testing:

- Group chat tests: the group named `测试`
- Private chat tests: the friend whose nickname is `aloha`

Do not send live test messages to other friends or production groups.

Before a live test, resolve the target through NapCat's `get_group_list` or
`get_friend_list` API and verify that the name matches exactly one target.
