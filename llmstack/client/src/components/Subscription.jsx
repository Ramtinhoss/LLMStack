import { Button, Paper, Stack, Typography } from "@mui/material";
import { useRecoilValue } from "recoil";
import {
  organizationState,
  profileSelector,
  profileFlagsSelector,
} from "../data/atoms";
import { formatStorage } from "../data/utils";

function Subscription(props) {
  const profileFlags = useRecoilValue(profileFlagsSelector);
  const profile = useRecoilValue(profileSelector);
  const organization = useRecoilValue(organizationState);

  return (
    <Stack sx={{ margin: "0 10px 60px 10px" }}>
      <Stack>
        <Typography
          variant="h6"
          className="section-header"
          sx={{ marginBottom: "8px" }}
        >
          Storage
          <span style={{ float: "right", fontWeight: 400, fontSize: "16px" }}>
            Used:{" "}
            <b>{`${
              Math.round(
                (profile?.credits?.used_storage /
                  profile?.credits?.total_storage) *
                  10000,
              ) / 100
            }% of ${formatStorage(profile?.credits?.total_storage)}`}</b>
          </span>
        </Typography>
        <br />
        <Typography
          variant="h6"
          className="section-header"
          sx={{ marginBottom: "8px" }}
        >
          Subscription
          <span style={{ float: "right", fontWeight: 400, fontSize: "16px" }}>
            Remaining Credits: <b>{profile?.credits?.available / 1000}</b>
          </span>
        </Typography>
        <Stack>
          <Paper>
            <Stack>
              <p
                style={{
                  display: profileFlags.IS_ORGANIZATION_MEMBER
                    ? "none"
                    : "block",
                }}
              >
                Logged in as&nbsp;<strong>{props.user_email}</strong>. You are
                currently subscribed to&nbsp;
                <strong>
                  {profileFlags.IS_PRO_SUBSCRIBER
                    ? "Pro"
                    : profileFlags.IS_BASIC_SUBSCRIBER
                      ? "Basic"
                      : "Free"}
                </strong>
                &nbsp;tier. Click on the Manage Subscription button below to
                change your plan.&nbsp;
                <br />
                <br />
                <i>
                  Note: You will be redirected to Stripe payment portal to
                  complete the upgrade payment process.
                </i>
              </p>
              <p
                style={{
                  display: profileFlags.IS_ORGANIZATION_MEMBER
                    ? "block"
                    : "none",
                }}
              >
                Logged in as <strong>{props.user_email}</strong>. Your account
                is managed by your organization,&nbsp;
                <strong>{organization?.name}</strong>. Please contact your admin
                to manage your subscription.
              </p>
            </Stack>
          </Paper>
        </Stack>
      </Stack>
      {!profileFlags.IS_ORGANIZATION_MEMBER && (
        <Button
          variant="contained"
          sx={{
            marginTop: "10px",
            display: profileFlags.IS_ORGANIZATION_MEMBER ? "none" : "inherit",
            alignSelf: "end",
          }}
          component="a"
          href={`${
            process.env.REACT_APP_SUBSCRIPTION_MANAGEMENT_URL
          }?prefilled_email=${encodeURIComponent(props.user_email)}`}
          target="_blank"
          rel="noreferrer"
        >
          Manage Subscription
        </Button>
      )}
    </Stack>
  );
}

export default Subscription;
